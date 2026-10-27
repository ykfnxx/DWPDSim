"""Run frozen, non-overwriting baseline/cost comparisons on prepared inputs.

Each case runs in a separate process with the same source/native snapshot.
The runner saves input hashes, source hashes and the binary before replay.
"""

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def summarize(paths, output):
    results = [json.loads(path.read_text()) for path in paths]
    for result in results:
        if not result["native_unchanged_during_run"] or not result["runtime_sources_unchanged"]:
            raise RuntimeError("runtime changed during an experiment")
    inputs = {(r["hashes_sha256"], r["index_sha256"], r["replayed_requests"],
               r["last_request_timestamp_ns"], r["stats"]["prefill"]["input_tokens"],
               r["native_extension_sha256"]) for r in results}
    if len(inputs) != 1:
        raise ValueError("comparison inputs, observation endpoints or native binaries differ")
    baselines = {}
    for result in results:
        cfg = result["config"]
        key = (cfg["storage_policy"]["kind"], cfg["slc"]["capacity_bytes"], cfg["tlc"]["capacity_bytes"])
        if result["memory_policy"] == "baseline":
            baselines[key] = result
    rows = []
    for path, result in zip(paths, results, strict=True):
        cfg, stats = result["config"], result["stats"]
        key = (cfg["storage_policy"]["kind"], cfg["slc"]["capacity_bytes"], cfg["tlc"]["capacity_bytes"])
        baseline = baselines[key]
        bcfg = baseline["config"]
        for field in ("memory", "slc", "tlc", "block_size_bytes", "storage_policy", "simulation_end_ns"):
            if cfg[field] != bcfg[field]:
                raise ValueError(f"paired {field} differs")
        for field in ("full_block_bytes", "linear_checkpoint_bytes", "checkpoint_generation",
                      "tokens_per_block", "lru_k", "admit_storage_hits", "replay_mode"):
            if cfg["hybrid_attention"][field] != bcfg["hybrid_attention"][field]:
                raise ValueError(f"paired hybrid {field} differs")
        writes = sum(stats["storage"][pool]["write_bytes"] for pool in ("slc", "tlc"))
        bwrites = sum(baseline["stats"]["storage"][pool]["write_bytes"] for pool in ("slc", "tlc"))
        compute = stats["prefill"]["prefill_compute_tokens"]
        bcompute = baseline["stats"]["prefill"]["prefill_compute_tokens"]
        rows.append({
            "case": path.parent.name, "memory_policy": result["memory_policy"],
            "storage_policy": key[0], "capacity_bytes": key[1]+key[2],
            "weight": cfg["hybrid_attention"]["cost"]["wear_weight"] if result["memory_policy"] == "joint_cost" else None,
            "requests": result["replayed_requests"], "input_tokens": stats["prefill"]["input_tokens"],
            "joint_token_hit_rate": stats["prefill"]["joint_token_hit_rate"],
            "compute_tokens": compute, "additional_compute_tokens": compute-bcompute,
            "allocated_host_write_bytes": writes, "host_write_ratio_to_baseline": writes/bwrites,
            "full_drop_objects": stats["full"]["drop_objects"],
            "linear_drop_objects": stats["linear"]["drop_objects"],
            "baseline_dominates_on_compute_and_host_writes": compute >= bcompute and writes >= bwrites and (compute > bcompute or writes > bwrites),
            "replay_seconds": result["replay_seconds"],
            "result_path": str(path.resolve()),
        })
    summary = {"scope": "paired logical replay; host writes include padding/migrations; no physical WAF or measured TTFT claim",
               "rows": rows}
    (output / "summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    with (output / "summary.csv").open("w") as target:
        writer = csv.DictWriter(target, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--end-seconds", type=float, default=3600)
    parser.add_argument("--capacities", nargs="+", type=int, choices=(1, 4), default=(1, 4))
    parser.add_argument("--storage-policies", nargs="+", choices=("wear_share_round_robin", "adaptive_endurance"),
                        default=("wear_share_round_robin", "adaptive_endurance"))
    parser.add_argument("--weights", nargs="+", type=float, default=(0, 1))
    parser.add_argument("--streams", type=int, default=1)
    parser.add_argument("--cost-candidates", type=int, default=1)
    parser.add_argument("--cost-scenarios", type=int, default=4)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cases = []
    for policy in args.storage_policies:
        for capacity in args.capacities:
            for weight in (None, *args.weights):
                name = f"{policy}-{capacity}tb-" + ("baseline" if weight is None else f"cost-w{weight:g}")
                command = [sys.executable, str(ROOT / "benchmark/prepared_hybrid_baseline.py"),
                           "--prepared", str(args.prepared.resolve()), "--output", str((args.output / name).resolve()),
                           "--storage-policy", policy, "--capacity-tb", str(capacity), "--memory-bytes", "100000000000",
                           "--end-seconds", str(args.end_seconds), "--streams", str(args.streams),
                           "--memory-policy", "baseline" if weight is None else "joint_cost",
                           "--cost-candidates", str(args.cost_candidates), "--cost-scenarios", str(args.cost_scenarios)]
                if weight is not None:
                    command += ["--cost-weight", str(weight)]
                cases.append({"name": name, "command": command})
    manifest = {"cases": cases, "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (args.output / "experiment.json").write_text(json.dumps(manifest, indent=2)+"\n")
    paths = []
    for case in cases:
        print(json.dumps({"phase": "starting", "case": case["name"]}), flush=True)
        with (args.output / (case["name"]+".log")).open("w") as log:
            subprocess.run(case["command"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        paths.append(args.output / case["name"] / "result.json")
        print(json.dumps({"phase": "completed", "case": case["name"]}), flush=True)
    summarize(paths, args.output)


if __name__ == "__main__":
    main()
