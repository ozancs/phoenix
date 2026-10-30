"""Strict linear-G-code index. Unsupported commands taint subsequent matches.

This is not a second Klipper interpreter or a resume-file generator. Byte offsets
and G-code coordinates are preserved; live machine transforms need separate proof.
"""
import hashlib
import math
import re

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
WORD = re.compile(r"([A-Z])\s*(" + NUMBER + r")", re.I)


def index_gcode(data, initial=(0., 0., 0., 0.)):
    if len(initial) != 4 or not all(math.isfinite(v) for v in initial):
        raise ValueError("Explicit finite initial XYZE required")
    pos, base = list(initial), [0.] * 4
    absolute, absolute_e, feed, flow, speed_factor = True, True, 25., 1., 1.
    offset, layer, layer_z = 0, None, None
    segments, unsupported = [], []
    for number, raw in enumerate(data.splitlines(keepends=True), 1):
        text = raw.decode("utf-8", errors="replace")
        marker = re.search(r";\s*LAYER:\s*(-?\d+)", text, re.I)
        if marker:
            layer = int(marker[1])
        if re.search(r";\s*(?:LAYER_CHANGE|CHANGE_LAYER)\b", text, re.I):
            layer = 0 if layer is None else layer+1
        line = text.split(";", 1)[0].strip().upper()
        end_offset = offset + len(raw)
        if not line:
            offset = end_offset
            continue
        words = WORD.findall(line)
        command = words[0][0] + str(int(float(words[0][1]))) if words and line.startswith(words[0][0]) else ""
        # Reject macros, checksums, arcs, expressions and nonintegral G/M commands.
        residue = WORD.sub("", line).strip()
        valid = not residue and words and words[0][0] in ("G", "M") and float(words[0][1]).is_integer()
        params = {k: float(v) for k, v in words[1:]} if valid else {}
        if not all(math.isfinite(v) for v in params.values()):
            raise ValueError("Nonfinite G-code parameter at line %d" % number)
        if not valid:
            command = "unsupported"
        if command in ("G0", "G1"):
            before = list(pos)
            if "F" in params:
                feed = params["F"]/60.
            if feed <= 0:
                raise ValueError("Nonpositive feed at line %d" % number)
            for i, axis in enumerate("XYZE"):
                if axis not in params:
                    continue
                value = params[axis] * (flow if i == 3 else 1.)
                is_abs = absolute and (i < 3 or absolute_e)
                pos[i] = value + base[i] if is_abs else pos[i] + value
            delta = [b-a for a, b in zip(before, pos)]
            if any(delta):
                kind = "retract" if delta[3] < 0 else "extrusion" if delta[3] > 0 else "travel"
                if kind == "extrusion" and any(delta[:2]):
                    layer_z = pos[2] if layer_z is None else max(layer_z, pos[2])
                segments.append({"id": len(segments), "line": number, "offset": offset,
                                 "end_offset": end_offset, "line_sha256": hashlib.sha256(raw).hexdigest(),
                                 "start": before, "end": list(pos), "kind": kind, "feed": feed*speed_factor,
                                 "layer": layer, "layer_z": layer_z,
                                 "z_hop_possible": layer_z is not None and max(before[2], pos[2]) > layer_z+1e-6,
                                 "state_supported": not unsupported,
                                 "modes": {"absolute": absolute, "absolute_e": absolute_e, "flow": flow}})
        elif command == "G90":
            absolute = True
        elif command == "G91":
            absolute = False
        elif command == "M82":
            absolute_e = True
        elif command == "M83":
            absolute_e = False
        elif command == "G92":
            for i, axis in enumerate("XYZE"):
                if axis in params or not params:
                    base[i] = pos[i] - params.get(axis, 0.) * (flow if i == 3 else 1.)
        elif command == "M220":
            speed_factor = params.get("S", 100.)/100.
            if speed_factor <= 0:
                raise ValueError("Invalid speed factor")
        elif command == "M221":
            new_flow = params.get("S", 100.)/100.
            if new_flow <= 0:
                raise ValueError("Invalid flow factor")
            base[3] = pos[3] - (pos[3]-base[3])*new_flow/flow
            flow = new_flow
        elif command not in ("M104", "M109", "M140", "M190", "M106", "M107"):
            unsupported.append({"line": number, "offset": offset, "command": line})
        offset = end_offset
    return {"schema": 1, "file_sha256": hashlib.sha256(data).hexdigest(), "file_size": len(data),
            "coordinate_frame": "gcode_with_G92_and_flow_only", "segments": segments,
            "unsupported": unsupported, "initial_position_assumed": list(initial),
            "timing": "requires_recorded_planner_phases_not_feedrate_alone"}
