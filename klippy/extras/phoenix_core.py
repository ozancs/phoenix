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
import time

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
                if (type(body.get("sequence")) is not int or body["sequence"] < 1
                        or not isinstance(body.get("payload"), dict)):
                    raise ValueError("Invalid journal generation or payload")
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


def run_disk_task(reactor, task):
    """Wait cooperatively; only pure disk/Python work may run in this worker.

    Klipper completion.wait yields its greenlet to the reactor. Event.wait/join
    on the reactor thread would reintroduce the scheduling stall we avoid here.
    """
    completion = reactor.completion()

    def work():
        try:
            outcome = (task(), None)
        except Exception as exc:
            outcome = (None, exc)
        reactor.async_complete(completion, outcome)

    threading.Thread(target=work, name="phoenix-startup-io", daemon=True).start()
    result, error = completion.wait()
    if error is not None:
        raise error
    return result


def initialize_recording(writer, journal, path, root, metadata, settings, replace, timings):
    """Disk transaction on a worker, with the sampling producer stopped.

    No printer/reactor/G-code APIs here. Do not publish recording until both
    generations are durable. An older job is invalidated before replacing it.
    """
    def measure(name, operation):
        start = time.monotonic()
        try:
            return operation()
        finally:
            timings[name + "_ms"] = round((time.monotonic()-start)*1000., 3)

    job = dict(metadata)
    job["file"] = measure("file_hash", lambda: file_identity(path, root))
    job["config_hash"] = measure("config_hash", lambda: digest(settings))
    measure("writer_drain", writer.flush)
    if replace:
        measure("old_recovery_close", lambda: journal.terminal("replaced"))
    measure("journal_slot_1", lambda: journal.save(job))
    measure("journal_slot_2", lambda: journal.save(job))
    measure("fence_clear", journal.clear_fence)
    return job


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


# Analysis is deliberately separate from motion authorization. These functions
# consume scheduled trajectories, never motor measurements.
def phase_position(phase, print_time):
    """Evaluate one constant-acceleration trapq phase (XYZ or nominal E)."""
    values = [phase[k] for k in ("time", "duration", "velocity", "acceleration")]
    values += list(phase["start"]) + list(phase["direction"]) + [print_time]
    if not all(math.isfinite(v) for v in values) or phase["duration"] < 0:
        raise ValueError("Invalid trajectory phase")
    if len(phase["start"]) != len(phase["direction"]) or not phase["start"]:
        raise ValueError("Invalid trajectory dimensions")
    t = max(0., min(phase["duration"], print_time - phase["time"]))
    distance = phase["velocity"] * t + .5 * phase["acceleration"] * t * t
    return [p + r * distance for p, r in zip(phase["start"], phase["direction"])]


def trajectory_envelope(phases, window, clock_error=0., mechanical_bounds=None):
    """Union every intersecting phase, including internal velocity-zero extrema.

    Uncovered times are unknown, not extrapolated. mechanical_bounds=None means
    there is NO physical bound, even when the planned envelope is very narrow.
    All times belong to one uninterrupted MCU print-time epoch.
    """
    if (len(window) != 2 or not all(math.isfinite(v) for v in window)
            or window[0] > window[1] or not math.isfinite(clock_error) or clock_error < 0):
        raise ValueError("Invalid interruption window")
    lo, hi = window[0] - clock_error, window[1] + clock_error
    points, covered, candidates = [], [], []
    for phase in sorted(phases, key=lambda p: p["time"]):
        phase_position(phase, phase["time"])  # Validate even nonintersecting input.
        a, b = max(lo, phase["time"]), min(hi, phase["time"] + phase["duration"])
        if a > b:
            continue
        times = [a, b]
        if phase["acceleration"]:
            turning = phase["time"] - phase["velocity"] / phase["acceleration"]
            if a < turning < b:
                times.append(turning)
        points.extend(phase_position(phase, t) for t in times)
        covered.append((a, b))
        if phase.get("segment") is not None:
            candidates.append(phase["segment"])
    cursor = lo
    complete = bool(covered)
    for a, b in covered:
        if a > cursor + 1e-9:
            complete = False
        cursor = max(cursor, b)
    complete = complete and cursor >= hi - 1e-9
    if not points:
        return {"complete": False, "planned_bounds": None, "physical_bounds": None,
                "estimate": None, "candidates": [], "window": [lo, hi]}
    dim = len(points[0])
    if any(len(p) != dim for p in points):
        raise ValueError("Mixed trajectory dimensions")
    bounds = [[min(p[i] for p in points), max(p[i] for p in points)] for i in range(dim)]
    physical = None
    if mechanical_bounds is not None:
        if (len(mechanical_bounds) != dim or any(not math.isfinite(v) or v < 0 for v in mechanical_bounds)):
            raise ValueError("Invalid mechanical error bounds")
        if complete:
            physical = [[a - e, b + e] for (a, b), e in zip(bounds, mechanical_bounds)]
    return {"complete": complete, "planned_bounds": bounds if complete else None,
            "physical_bounds": physical, "estimate": [(a+b)/2 for a, b in bounds] if complete else None,
            "candidates": sorted(set(candidates)), "window": [lo, hi]}


def match_segments(segments, bounds, offset_range=None, time_window=None, kind=None,
                   direction=None, layer=None, sequence_range=None):
    """Keep ALL possible intersections of a line segment with a position box.

    Supplied constraints must be evidence-backed; no nearest-point tie breaking.
    Repeated visits, pure E retracts and Z hops remain distinct sequence IDs.
    Coordinate frame must match the evidence (no implicit mesh/offset conversion).
    """
    if not bounds or any(len(b) != 2 or not all(math.isfinite(v) for v in b) or b[0] > b[1] for b in bounds):
        raise ValueError("Invalid position bounds")
    matches = []
    for s in segments:
        if s.get("state_supported") is False:
            continue
        if offset_range and not (s["offset"] <= offset_range[1] and s["end_offset"] >= offset_range[0]):
            continue
        if sequence_range and not sequence_range[0] <= s["id"] <= sequence_range[1]:
            continue
        if kind and s["kind"] != kind or layer is not None and s.get("layer") != layer:
            continue
        if time_window and "time" in s and (s["time"] > time_window[1] or s["end_time"] < time_window[0]):
            continue
        delta = [b-a for a, b in zip(s["start"], s["end"])]
        if direction and sum(a*b for a, b in zip(delta, direction)) < 0:
            continue
        a, b = 0., 1.
        for i, (low, high) in enumerate(bounds):
            if not delta[i]:
                if not low <= s["start"][i] <= high:
                    a, b = 1., 0.
                    break
            else:
                u, v = (low-s["start"][i])/delta[i], (high-s["start"][i])/delta[i]
                a, b = max(a, min(u, v)), min(b, max(u, v))
        if a <= b:
            matches.append({"id": s["id"], "offset": s["offset"], "end_offset": s["end_offset"],
                            "fraction": [a, b], "kind": s["kind"], "layer": s.get("layer"),
                            "previous": s["id"]-1, "next": s["id"]+1})
    return matches


def assess_evidence(evidence):
    """Evidence classes, not probabilities. High confidence never auto-starts."""
    unsafe = [key for key in ("file_changed", "config_changed", "storage_failed", "no_valid_checkpoint",
                              "axes_moved", "z_dropped", "collision", "epoch_mismatch", "state_unsupported")
              if evidence.get(key)]
    missing = [key for key in ("timing_complete", "stop_window_known", "physical_verified",
                               "extrusion_verified", "state_verified", "unique_segment", "within_tolerance")
               if evidence.get(key) is not True]
    level = "unsafe" if unsafe else "uncertain" if missing else "high"
    return {"class": level, "reasons": unsafe or missing, "automatic_resume": False,
            "action": "recovery_unavailable" if unsafe else "manual_verification" if missing else "confirm_resume"}


def infer_homing_origin(after, step_delta, step_distance, kinematics="cartesian"):
    """Research-only inverse displacement, never sent to SET_KINEMATIC_POSITION.

    step_delta is before_count - after_count within ONE continuous counter epoch.
    Counts are commanded steps; no guarantee that the motor executed them.
    """
    if kinematics not in ("cartesian", "corexy"):
        raise ValueError("Unsupported inverse kinematics")
    if any(len(v) != 3 for v in (after, step_delta, step_distance)):
        raise ValueError("Expected three motor axes")
    if not all(math.isfinite(v) for arr in (after, step_delta, step_distance) for v in arr):
        raise ValueError("Invalid homing observation")
    if any(d <= 0 for d in step_distance):
        raise ValueError("Invalid step distance")
    a, b, z = [d*s for d, s in zip(step_delta, step_distance)]
    delta = [(a+b)/2, (a-b)/2, z] if kinematics == "corexy" else [a, b, z]
    return [p+d for p, d in zip(after, delta)]


def capture_motion_evidence(printer, eventtime, limit=32):
    """Bounded read-only Klipper adapter. Never flushes lookahead or commands motion.

    Private extraction API is feature-detected. Its output has no exact SD-line
    attribution; a sampled offset is a command boundary, not an executed boundary.
    """
    result = {"schema": 1, "kind": "scheduled_not_measured", "host_monotonic": eventtime,
              "stop_time_known": False, "offset_mapping": "checkpoint_boundary_only"}
    try:
        report = printer.lookup_object("motion_report", None)
        mcu = printer.lookup_object("mcu", None)
        if report is None or mcu is None:
            result["unavailable"] = "motion_report or MCU unavailable"
            return result
        now = mcu.estimated_print_time(eventtime)
        result["estimated_print_time"] = now
        result["clock_error_bound"] = None  # clocksync regression is not a certified bound.
        import chelper
        ffi, lib = chelper.get_ffi()
        tracks = {}
        for name in ("toolhead", "extruder"):
            dump = report.dtrapqs.get(name)
            if dump is None:
                continue
            data = ffi.new("struct pull_move[%d]" % limit)
            count = lib.trapq_extract_old(dump.trapq, data, limit, max(0., now-5.), now+5.)
            records = []
            for i in range(count-1, -1, -1):
                m = data[i]
                records.append({"time": m.print_time, "duration": m.move_t,
                                "velocity": m.start_v, "acceleration": m.accel,
                                "start": [m.start_x, m.start_y, m.start_z],
                                "direction": [m.x_r, m.y_r, m.z_r]})
            tracks[name] = {"phases": records, "possibly_truncated": count == limit}
        result["tracks"] = tracks
        # Round-trip ensures no NaN/Infinity or borrowed C storage enters journal.
        canonical(result)
    except Exception as exc:
        result = {"schema": 1, "kind": "scheduled_not_measured", "stop_time_known": False,
                  "unavailable": type(exc).__name__ + ": " + str(exc)}
    return result
