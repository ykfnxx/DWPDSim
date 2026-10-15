"""Compare context LRU with the frozen no-hint residency experiment."""

import argparse
import csv
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import reuse_policy_experiment as replay

from dwpdsim import _core

OUT = replay.ROOT / "build/context-nohint"
REFERENCE = replay.ROOT / "build/residency-nohint"


def options(beta):
    return {
        "kind": "context_lru",
        "admit_storage_hits": True,
        "alpha": 0.1,
        "max_eviction_blocks": 64,
        "retention_ns": 300 * 10**9,
        "retention_growth_seconds_per_block": beta,
    }


def summarize():
    rows = []
    for dataset in replay.SOURCES:
        for capacity in [16384, 32768]:
            baseline = json.loads(
                (REFERENCE / dataset / f"{capacity}-dump-all/result.json").read_text()
            )
            bs = baseline["stats"]
            for name, folder in [
                ("dump-all", REFERENCE),
                ("lru-300", REFERENCE),
                ("value-60", REFERENCE),
                ("value-300", REFERENCE),
                ("context-b0", OUT),
                ("context-b20", OUT),
            ]:
                r = json.loads((folder / dataset / f"{capacity}-{name}/result.json").read_text())
                a = r["stats"]["accesses"]
                writes = r["stats"]["storage"]["tlc"]["writes"]["blocks"]
                rows.append(
                    {
                        "dataset": dataset,
                        "memory_gib": capacity / 128,
                        "variant": name,
                        "hit_pct": 100 * (1 - a["global_misses"] / a["total"]),
                        "storage_write_blocks": writes,
                        "storage_write_tib": writes / 131072,
                        "storage_write_change_pct": 100
                        * (writes / bs["storage"]["tlc"]["writes"]["blocks"] - 1),
                        "compute_cost": a["compute_cost"],
                        "compute_change_pct": 100
                        * (a["compute_cost"] / bs["accesses"]["compute_cost"] - 1),
                    }
                )
    (OUT / "comparison.json").write_text(json.dumps(rows, indent=2) + "\n")
    with (OUT / "comparison.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["run", "worker", "summarize"])
    p.add_argument("--dataset", choices=list(replay.SOURCES))
    p.add_argument("--capacity", type=int)
    p.add_argument("--beta", type=int, choices=[0, 20])
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()
    if args.mode == "worker":
        replay.measure(
            OUT, args.dataset, args.capacity, f"context-b{args.beta}", options=options(args.beta)
        )
        return
    if args.mode == "summarize":
        summarize()
        return
    OUT.mkdir(exist_ok=True)
    native = Path(_core.__file__)
    manifest = {
        "reference": str(REFERENCE),
        "native_sha256": replay.sha(native),
        "native_path": str(native),
        "no_hint": True,
        "options": {str(beta): options(beta) for beta in [0, 20]},
        "datasets": {},
    }
    expected = json.loads((REFERENCE / "runtime.json").read_text())
    assert manifest["native_sha256"] == expected["sha256"]
    for dataset in replay.SOURCES:
        dest = OUT / dataset
        dest.mkdir(exist_ok=True)
        for filename in ["evaluation.parquet", "requests.npy"]:
            target = dest / filename
            if not target.exists():
                target.symlink_to(REFERENCE / dataset / filename)
        manifest["datasets"][dataset] = replay.sha(dest / "evaluation.parquet")
    (OUT / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def launch(job):
        dataset, capacity, beta = job
        dest = OUT / dataset / f"{capacity}-context-b{beta}"
        dest.mkdir(exist_ok=True)
        if (dest / "result.json").exists():
            return
        with (dest / "run.log").open("w") as f:
            subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "worker",
                    "--dataset",
                    dataset,
                    "--capacity",
                    str(capacity),
                    "--beta",
                    str(beta),
                ],
                stdout=f,
                stderr=subprocess.STDOUT,
                check=True,
            )
        print("DONE", dataset, capacity, beta, flush=True)

    jobs = [(d, c, beta) for d in replay.SOURCES for c in [16384, 32768] for beta in [0, 20]]
    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(launch, jobs))
    summarize()


if __name__ == "__main__":
    main()
