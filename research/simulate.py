"""Deterministic offline experiment. No printer, network or heater commands.

python -m research.simulate --klipper PATH --competitor PATH --cases 6600
"""
import argparse
import copy
import gzip
import importlib.util
import json
import math
from pathlib import Path
import random
import subprocess

from klippy.extras.phoenix_core import trajectory_envelope, match_segments, assess_evidence
from research.gcode_index import index_gcode
from research.planner import plan, ground_truth

SCENARIOS = ("before_motion", "acceleration", "cruise", "deceleration", "extrusion",
             "travel", "retract", "layer_change", "z_hop", "queue_delay", "clock_drift",
             "repeated_xy", "crossing", "lost_steps_hidden", "xy_shift_hidden", "z_drop_reported",
             "corrupt_checkpoint", "torn_write", "mcu_reset", "host_reset", "missing_log",
             "modified_file", "verified_synthetic", "pressure_advance", "xy_shift_reported")


def fixture():
    lines = ["; Phoenix reproducible fixture", "G90", "M82", "G92 E0", "G1 X20 Y20 Z0.2 F6000"]
    e = 0.
    for layer in range(3):
        z = .2*(layer+1)
        lines += [";LAYER:%d" % layer, "G1 Z%.3f F600" % z]
        # Multiple visits to the same geometry, directions and crossings.
        for _ in range(3):
            for x, y in ((180, 20), (20, 180), (180, 180), (20, 20)):
                e += 4.
                lines.append("G1 X%d Y%d E%.4f F7200" % (x, y, e))
        e -= 1.
        lines += ["G1 E%.4f F1500" % e, "G1 Z%.3f F600" % (z+.6), "G1 X100 Y100 F9000",
                  "G1 Z%.3f F600" % z]
        e += 1.
        lines += ["G1 E%.4f F1500" % e, "G1 X20 Y20 F9000"]
    return ("\n".join(lines)+"\n").encode()


def quantiles(values):
    values = sorted(values)
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": values[len(values)//2],
            "p95": values[min(len(values)-1, int(len(values)*.95))], "max": values[-1]}


def revision(path):
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def run(klipper, competitor, output, cases=6600, seed=20261030):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    data = fixture()
    fixture_path = output / "fixture.gcode"
    fixture_path.write_bytes(data)
    indexed = index_gcode(data)
    segments = indexed["segments"]
    phases = plan(segments, klipper)
    for s in segments:
        last = next(p for p in reversed(phases) if p["segment"] == s["id"])
        endpoint = ground_truth(last, last["time"]+last["duration"])
        if max(abs(a-b) for a, b in zip(endpoint, s["end"])) > 1e-7:
            raise ValueError("Upstream planner endpoint mismatch")
    hold = {"time": phases[0]["time"]-1, "duration": 1., "start": phases[0]["start"],
            "direction": [0.]*4, "velocity": 0., "acceleration": 0., "phase": "hold", "segment": 0}
    phases.insert(0, hold)
    matcher = None
    if competitor:
        path = Path(competitor) / "klippy/extras/lost_z_make_resume.py"
        spec = importlib.util.spec_from_file_location("upstream_matcher", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        matcher = module.find_crash_move
    rng = random.Random(seed)
    all_errors, endpoint_errors, layer_errors, per_scenario = [], [], [], {}
    axis_errors = [[] for _ in range(4)]
    misses = unknown = ambiguous = false_high = high_count = unique_correct = 0
    mike_correct = mike_trials = 0
    geometry_contains = geometry_ambiguous = geometry_unique_correct = 0
    failures = []
    with (output / "cases.jsonl.gz").open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as logs:
            for i in range(cases):
                scenario = SCENARIOS[i % len(SCENARIOS)]
                pool = phases[1:]
                if scenario in ("acceleration", "cruise", "deceleration"):
                    phase_name = {"acceleration": "accel", "cruise": "cruise", "deceleration": "decel"}[scenario]
                    pool = [p for p in pool if p["phase"] == phase_name]
                elif scenario in ("extrusion", "travel", "retract"):
                    pool = [p for p in pool if segments[p["segment"]]["kind"] == scenario]
                elif scenario in ("layer_change", "z_hop"):
                    pool = [p for p in pool if abs(p["direction"][2]) > .01]
                elif scenario == "before_motion":
                    pool = [hold]
                p = rng.choice(pool)
                t = p["time"] + p["duration"]*rng.uniform(.05, .95)
                truth = ground_truth(p, t)
                offset = segments[p["segment"]]["offset"]
                physical_shift = [0.]*4
                flags = {}
                if scenario in ("lost_steps_hidden", "xy_shift_hidden", "xy_shift_reported"):
                    physical_shift[0] = rng.uniform(.3, 4.)
                    if scenario == "xy_shift_reported":
                        flags["axes_moved"] = True
                elif scenario == "z_drop_reported":
                    physical_shift[2] = rng.uniform(.5, 10.)
                    flags["z_dropped"] = True
                elif scenario == "pressure_advance":
                    physical_shift[3] = .04 * (p["velocity"]+p["acceleration"]*(t-p["time"]))*p["direction"][3]
                truth = [v+d for v, d in zip(truth, physical_shift)]
                # Observer gets time uncertainty and reported faults, never hidden displacement.
                halfwidth = rng.uniform(.0001, .004)
                if scenario == "queue_delay":
                    halfwidth = .6
                ppm = rng.uniform(-80., 80.) if scenario == "clock_drift" else 0.
                since_sync = rng.uniform(1., 30.) if scenario == "clock_drift" else 0.
                drift = ppm*1e-6*since_sync
                clock_error = 80e-6*since_sync if scenario == "clock_drift" else .0001
                window = [t+drift-halfwidth, t+drift+halfwidth]
                evidence_phases = phases
                if scenario in ("repeated_xy", "crossing"):
                    window = [phases[0]["time"]+.01, phases[-1]["time"]+phases[-1]["duration"]-.01]
                if scenario in ("host_reset", "missing_log", "torn_write"):
                    evidence_phases = [x for x in phases if x["time"]+x["duration"] < t-.1]
                if scenario == "corrupt_checkpoint":
                    flags["no_valid_checkpoint"] = True
                    evidence_phases = []
                if scenario == "mcu_reset":
                    flags["epoch_mismatch"] = True
                    evidence_phases = []
                if scenario == "modified_file":
                    flags["file_changed"] = True
                verified = scenario == "verified_synthetic"
                result = trajectory_envelope(evidence_phases, window, clock_error,
                                             [.01]*4 if verified else None)
                bounds = result["planned_bounds"]
                candidates = match_segments(segments, bounds, time_window=result["window"]) if bounds else []
                ids = {m["id"] for m in candidates}
                tolerance_bounds = result["physical_bounds"] if verified else bounds
                flags.update(timing_complete=result["complete"], stop_window_known=True,
                             physical_verified=verified, extrusion_verified=verified,
                             state_verified=True, unique_segment=len(ids) == 1,
                             within_tolerance=bool(tolerance_bounds) and all(b-a <= .2 for a, b in tolerance_bounds))
                decision = assess_evidence(flags)
                error = None
                contains = None
                if result["estimate"] is not None:
                    error = [abs(a-b) for a, b in zip(result["estimate"], truth)]
                    all_errors.append(math.dist(result["estimate"][:3], truth[:3]))
                    for axis in range(4):
                        axis_errors[axis].append(error[axis])
                    contains = all(a-1e-7 <= v <= b+1e-7 for (a, b), v in zip(bounds, truth))
                    if not physical_shift or not any(physical_shift):
                        if not contains:
                            failures.append({"case": i, "error": "planned envelope missed modeled truth"})
                    if p["segment"] not in ids and scenario != "before_motion":
                        misses += 1
                    if len(ids) == 1 and p["segment"] in ids:
                        unique_correct += 1
                    ambiguous += len(ids) > 1
                else:
                    unknown += 1
                if decision["class"] == "high":
                    high_count += 1
                    if not contains or max(error) > .1 or p["segment"] not in ids or any(flags.get(k) for k in ("file_changed", "epoch_mismatch")):
                        false_high += 1
                # Explicit proxy baselines, not execution of YUMI/BTT/ankur recovery.
                parsed = next((s for s in segments if s["time"] >= t+.4), segments[-1])
                endpoint_error = math.dist(parsed["end"][:3], truth[:3])
                endpoint_errors.append(endpoint_error)
                layer_start = next(s for s in segments if s["layer"] == segments[p["segment"]]["layer"])
                layer_errors.append(math.dist(layer_start["start"][:3], truth[:3]))
                mike = None
                geometry = []
                if matcher and scenario != "before_motion":
                    # Actual upstream matcher, given exact simulated measured XYZ.
                    # Its real homing/thermal procedure is NOT run or validated.
                    mike = matcher(str(fixture_path), 0, data.count(b"\n"), truth[0], truth[1],
                                   near_idx=parsed["line"]-1, z=truth[2])
                    mike_trials += 1
                    mike_correct += bool(mike and mike[1] == segments[p["segment"]]["line"]-1)
                    # Equal information alternative: the same XYZ observation,
                    # NO timing and NO E coordinate. Keep all geometric candidates.
                    geometry = match_segments(segments, [[v-.1, v+.1] for v in truth[:3]])
                    geometry_ids = {m["id"] for m in geometry}
                    geometry_contains += p["segment"] in geometry_ids
                    geometry_ambiguous += len(geometry_ids) > 1
                    geometry_unique_correct += len(geometry_ids) == 1 and p["segment"] in geometry_ids
                stat = per_scenario.setdefault(scenario, {"cases": 0, "high": 0, "uncertain": 0, "unsafe": 0,
                                                          "unknown": 0, "errors": []})
                stat["cases"] += 1
                stat[decision["class"]] += 1
                stat["unknown"] += error is None
                if error is not None:
                    stat["errors"].append(math.sqrt(sum(v*v for v in error[:3])))
                record = {"case": i, "scenario": scenario, "time": t, "ground_truth": truth,
                          "pipeline": {"host_parsed_offset": parsed["end_offset"],
                                       "mcu_scheduled_until_assumed": t+.25,
                                       "clock_frequency_error_ppm": ppm, "seconds_since_sync": since_sync,
                                       "transport_model": "bounded_lookahead_not_packet_simulation"},
                          "true_segment": p["segment"], "offset": offset, "estimate": result,
                          "absolute_error_xyze_mm": error, "candidate_ids": sorted(ids),
                          "confidence": decision, "endpoint_proxy_xyz_error_mm": endpoint_error,
                          "planned_bounds_contain_physical_truth": contains,
                          "phoenix_geometry_only_candidates": [m["id"] for m in geometry],
                          "upstream_mike_match": mike}
                logs.write((json.dumps(record, sort_keys=True, allow_nan=False)+"\n").encode())
    for stat in per_scenario.values():
        stat["xyz_error_mm"] = quantiles(stat.pop("errors"))
    summary = {"seed": seed, "cases": cases, "klipper_commit": revision(klipper),
               "competitor_commit": revision(competitor) if competitor else None,
               "fixture_sha256": indexed["file_sha256"], "planner": "upstream Move/LookAheadQueue",
               "virtual_mcu": False, "physical_hardware_tested": False,
               "planner_endpoints_verified": len(segments),
               "xyz_estimate_error_mm": quantiles(all_errors), "unknown_estimates": unknown,
               "axis_error_mm": {axis: quantiles(errors) for axis, errors in zip("XYZE", axis_errors)},
               "ambiguous_cases": ambiguous, "true_segment_missing_when_estimate_available": misses,
               "unique_correct_cases": unique_correct, "high_confidence_cases": high_count,
               "false_high_cases": false_high, "false_high_rate": false_high/high_count if high_count else None,
               "endpoint_proxy_xyz_error_mm": quantiles(endpoint_errors),
               "layer_start_proxy_xyz_error_mm": quantiles(layer_errors),
               "mike_actual_matcher": {"trials": mike_trials, "exact_line_correct": mike_correct,
                                       "measurement": "synthetic actual XYZ, not inferred by real homing"},
               "phoenix_geometry_only": {"trials": mike_trials, "truth_retained": geometry_contains,
                                          "ambiguous": geometry_ambiguous, "unique_correct": geometry_unique_correct,
                                          "tolerance_xyz_mm": .1, "timing_or_E_supplied": False},
               "by_scenario": per_scenario, "assertion_failures": failures,
               "limitations": ["No MCU/step compression or physical motors", "synthetic stop-time intervals",
                               "extruder pressure/mesh/input shaping not modeled", "high requires synthetic independent verification",
                               "unreported displacement is not detectable from scheduled motion",
                               "proxy baselines are not competitor recovery success rates"]}
    (output / "summary.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("by_scenario", "limitations")}, indent=2))
    if failures or false_high or misses:
        raise SystemExit("Simulation regression failed")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--klipper", required=True)
    parser.add_argument("--competitor")
    parser.add_argument("--output", default="research/results")
    parser.add_argument("--cases", type=int, default=6600)
    parser.add_argument("--seed", type=int, default=20261030)
    args = parser.parse_args()
    run(args.klipper, args.competitor, args.output, args.cases, args.seed)
