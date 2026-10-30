"""Inverse-homing sensitivity experiment, no printer homing commands."""
import json
from pathlib import Path
import random
from klippy.extras.phoenix_core import infer_homing_origin
from research.simulate import quantiles


def run(output="research/results", seed=20261031):
    rng = random.Random(seed)
    scenarios = ("quantization_only", "switch_repeatability", "trigger_latency",
                 "sensorless", "missed_steps", "unpowered_shift", "z_drop")
    results, rows = {}, []
    for kinematics in ("cartesian", "corexy"):
        for scenario in scenarios:
            errors = []
            for n in range(200):
                before_loss = [rng.uniform(10, 200) for _ in range(3)]
                at_boot = list(before_loss)
                if scenario == "unpowered_shift":
                    at_boot[0] += rng.uniform(-3, 3)
                if scenario == "z_drop":
                    at_boot[2] -= rng.uniform(.5, 10)
                sensor = [0., 0., 0.]
                if scenario == "switch_repeatability":
                    sensor = [rng.uniform(-.05, .05) for _ in range(3)]
                if scenario == "trigger_latency":
                    sensor = [50*rng.uniform(0, .002) for _ in range(3)]
                if scenario == "sensorless":
                    sensor = [rng.uniform(-1., 1.) for _ in range(3)]
                displacement = [x-y for x, y in zip(at_boot, sensor)]
                if kinematics == "corexy":
                    x, y, z = displacement
                    displacement = [x+y, x-y, z]
                distances = [.0125, .0125, .0025]
                counts = [round(v/d) for v, d in zip(displacement, distances)]
                if scenario == "missed_steps":
                    counts[0] += rng.randint(10, 200)
                estimate = infer_homing_origin([0., 0., 0.], counts, distances, kinematics)
                error = max(abs(a-b) for a, b in zip(estimate, before_loss))
                errors.append(error)
                rows.append({"kinematics": kinematics, "scenario": scenario, "case": n,
                             "pre_loss_truth": before_loss, "boot_truth": at_boot,
                             "inferred_origin": estimate, "max_axis_error_mm": error})
            results[kinematics+":"+scenario] = quantiles(errors)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output/"homing-cases.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows), encoding="utf-8")
    report = {"seed": seed, "cases": len(rows), "results": results,
              "assumptions_not_measurements": {"step_mm": [.0125, .0125, .0025],
                                               "switch_error_mm": .05, "latency_max_s": .002,
                                               "homing_speed_mm_s": 50, "sensorless_error_mm": 1.},
              "conclusion": "Commanded counts infer boot position conditionally; unpowered motion before boot is unobservable. No automatic compensation."}
    (output/"homing-summary.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run()
