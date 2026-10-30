"""Phoenix persistence and recovery planning; no Klipper dependencies.

Copyright (C) 2026 Phoenix for Klipper contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import threading

SCHEMA = 1


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_identity(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    relative = path.relative_to(root)
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return {"relative_path": relative.as_posix(), "size": path.stat().st_size,
            "sha256": sha.hexdigest()}


def verify_file(identity, root, offset):
    path = (Path(root) / identity["relative_path"]).resolve()
    if file_identity(path, root) != identity:
        raise ValueError("G-code file changed; recovery refused")
    if not isinstance(offset, int) or not 0 <= offset < identity["size"]:
        raise ValueError("Resume offset is outside the file")
    with path.open("rb") as stream:
        if offset:
            stream.seek(offset - 1)
            if stream.read(1) != b"\n":
                raise ValueError("Resume offset is not a complete line boundary")
    return path


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    if os.name == "posix":
        descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class Journal:
    """Two checksummed generations. Recovery motion has a separate durable fence."""
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        self.sequence = 0
        self.fallback = False
        self.load()

    @property
    def fence(self):
        return self.directory / "recovery-in-progress.json"

    def load(self):
        valid, invalid = [], 0
        for slot in (0, 1):
            path = self.directory / ("journal-%d.json" % slot)
            if not path.exists():
                continue
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                body = envelope["body"]
                if body["schema"] != SCHEMA or digest(body) != envelope["sha256"]:
                    raise ValueError("Invalid journal checksum or version")
                valid.append(body)
            except (ValueError, KeyError, TypeError):
                invalid += 1
        self.fallback = bool(invalid)
        if not valid:
            if invalid:
                raise ValueError("No valid Phoenix journal; preserve files for diagnosis")
            return None
        latest = max(valid, key=lambda entry: entry["sequence"])
        self.sequence = latest["sequence"]
        return latest["payload"]

    def save(self, payload):
        sequence = self.sequence + 1
        body = {"schema": SCHEMA, "sequence": sequence, "payload": payload}
        envelope = {"body": body, "sha256": digest(body)}
        atomic_write(self.directory / ("journal-%d.json" % (sequence % 2)),
                     canonical(envelope))
        self.sequence = sequence

    def terminal(self, phase):
        # Both slots become tombstones: a corrupt newest slot cannot resurrect a job.
        for _ in range(2):
            self.save({"phase": phase, "checkpoints": []})

    def fence_motion(self):
        atomic_write(self.fence, canonical({"reason": "Recovery preparation moved axes"}))

    def clear_fence(self):
        if self.fence.exists():
            self.fence.unlink()
            if os.name == "posix":
                descriptor = os.open(str(self.directory), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)


class JournalWriter:
    """Coalesce pending samples; never fsync on the motion reactor's timer path."""
    def __init__(self, journal):
        self.journal = journal
        self.error = None
        self.pending = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._run, name="phoenix-journal", daemon=True)
        self.thread.start()

    def submit(self, payload):
        payload = copy.deepcopy(payload)
        try:
            self.pending.get_nowait()
            self.pending.task_done()
        except queue.Empty:
            pass
        self.pending.put_nowait(payload)

    def _run(self):
        while True:
            payload = self.pending.get()
            try:
                self.journal.save(payload)
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.pending.task_done()

    def flush(self):
        self.pending.join()
        if self.error:
            raise ValueError("Phoenix storage failed: " + self.error)


def validate_snapshot(snapshot):
    position = snapshot["machine_position"]
    if len(position) != 4 or not all(math.isfinite(v) for v in position):
        raise ValueError("Invalid machine coordinates")
    state = snapshot["gcode_state"]
    for name in ("base_position", "last_position", "homing_position"):
        if len(state[name]) != 4 or not all(math.isfinite(v) for v in state[name]):
            raise ValueError("Unsupported coordinate state")
    for name in ("speed", "speed_factor", "extrude_factor"):
        if not math.isfinite(state[name]) or state[name] <= 0:
            raise ValueError("Invalid feed/extrusion state")


def recovery_plan(latest, selected, clearance, park, limits):
    """Current position ALWAYS comes from latest, never the selected older point.

    All positions are machine/toolhead coordinates, not G-code coordinates.
    Lift above both endpoints, traverse at clearance, then descend separately.
    """
    validate_snapshot(latest)
    validate_snapshot(selected)
    current = latest["machine_position"][:3]
    target = selected["machine_position"][:3]
    safe_z = max(current[2], target[2]) + clearance
    points = {"assumed": current, "lift": [current[0], current[1], safe_z],
              "park": [park[0], park[1], safe_z],
              "above_target": [target[0], target[1], safe_z], "target": target}
    if not math.isfinite(clearance) or clearance <= 0:
        raise ValueError("Clearance must be positive")
    for label, point in points.items():
        for axis, value in enumerate(point):
            if not math.isfinite(value) or not limits[0][axis] <= value <= limits[1][axis]:
                raise ValueError("%s exceeds %s travel limits" % (label, "XYZ"[axis]))
    return points


def demo_plan(current, limits, clearance):
    """Empty-bed demonstration waypoints, bounded by actual machine limits."""
    low, high = [list(edge)[:3] for edge in limits]
    if (len(current) < 3 or len(low) != 3 or len(high) != 3
            or not all(math.isfinite(v) for v in list(current[:3]) + low + high)
            or not math.isfinite(clearance) or clearance <= 0):
        raise ValueError("Invalid printer travel limits")
    span = [hi - lo for lo, hi in zip(low, high)]
    if any(value <= 0 for value in span):
        raise ValueError("Printer travel range is empty")
    if any(not low[i] <= current[i] <= high[i] for i in range(3)):
        raise ValueError("Current position is outside the printer travel limits")
    margin = min(clearance, span[2] * .1)
    origin = [low[0] + span[0] * .6, low[1] + span[1] * .55,
              low[2] + min(max(10., margin * 2), span[2] * .3)]
    initial_z = min(max(current[2], origin[2]) + margin, high[2])
    travel_z = origin[2] + margin
    return {"initial_lift": [current[0], current[1], initial_z],
            "initial_above": [origin[0], origin[1], initial_z], "origin": origin,
            "lift": [origin[0], origin[1], travel_z],
            "park": [low[0] + span[0] * .1, low[1] + span[1] * .1, travel_z],
            "above_origin": [origin[0], origin[1], travel_z]}
