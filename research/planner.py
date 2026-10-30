"""Execute real upstream Move/LookAheadQueue without importing Klipper hardware.

AST selects those two unmodified classes from a pinned local checkout. This is
real lookahead, not virtual MCU, step compression, input shaping or a printer.
"""
import ast
import math
from pathlib import Path
from types import SimpleNamespace


def plan(segments, klipper, acceleration=1000., velocity=150., start_time=10.):
    path = Path(klipper) / "klippy/toolhead.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name in ("Move", "LookAheadQueue")]
    if len(classes) != 2:
        raise ValueError("Unsupported Klipper planner API")
    namespace = {"math": math, "LOOKAHEAD_FLUSH_TIME": .150}
    exec(compile(ast.Module(body=classes, type_ignores=[]), str(path), "exec"), namespace)
    # Extruder pressure/temperature/flow limits are NOT simulated by this stub.
    extra = SimpleNamespace(calc_junction=lambda prev, current, index: 1e12)
    toolhead = SimpleNamespace(max_accel=acceleration, max_velocity=velocity,
                               junction_deviation=.02, mcr_pseudo_accel=acceleration*.5,
                               extra_axes=[extra])
    queue, moves = namespace["LookAheadQueue"](), []
    for s in segments:
        move = namespace["Move"](toolhead, s["start"], s["end"], s["feed"])
        # Explicit fixture limits on extruder-only travel; not a real extruder model.
        if not move.is_kinematic_move:
            move.limit_speed(25., 500.)
        move.segment = s
        queue.add_move(move)
    moves = queue.flush()
    phases, t = [], start_time
    for move in moves:
        s = move.segment
        s["time"] = t
        pos = list(move.start_pos)
        for name, duration, velocity0, accel in (("accel", move.accel_t, move.start_v, move.accel),
                                                ("cruise", move.cruise_t, move.cruise_v, 0.),
                                                ("decel", move.decel_t, move.cruise_v, -move.accel)):
            if duration > 1e-12:
                phases.append({"time": t, "duration": duration, "velocity": velocity0,
                               "acceleration": accel, "start": list(pos), "direction": list(move.axes_r),
                               "segment": s["id"], "phase": name, "offset": s["offset"]})
                distance = velocity0*duration + accel*duration**2/2
                pos = [p+r*distance for p, r in zip(pos, move.axes_r)]
                t += duration
        s["end_time"] = t
    return phases


def ground_truth(phase, t, substeps=200):
    """Independent numerical integration of velocity; no estimator calls."""
    duration = max(0., min(phase["duration"], t-phase["time"]))
    dt, velocity = duration/substeps, phase["velocity"]
    pos = list(phase["start"])
    for _ in range(substeps):
        next_velocity = velocity + phase["acceleration"]*dt
        for i, ratio in enumerate(phase["direction"]):
            pos[i] += (velocity + next_velocity)*.5*dt*ratio
        velocity = next_velocity
    return pos
