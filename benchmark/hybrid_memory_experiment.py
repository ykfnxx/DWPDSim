"""Causal synthetic backend replay; counts logical I/O, not measured TTFT."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import subprocess
import time
from pathlib import Path

from dwpdsim import (
    DWPDSimulator,
    HybridAttentionConfig,
    HybridEvent,
    MemoryConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)

ROOT = Path(__file__).resolve().parents[1]


def prepare(args):
    args.output.mkdir(parents=True, exist_ok=True)
    files = {}
    for seed in range(args.seeds):
        rng = random.Random(seed)
        paths = [[1, 2 + sid] for sid in range(args.sessions)]
        next_hash = args.sessions + 2
        seq = 0
        dest = args.output / f"events-{seed}.jsonl"
        with dest.open("w") as out:
            def emit(action, path, now, **kwargs):
                nonlocal seq
                row = dict(timestamp_ns=now, sequence=seq, action=action,
                           hash_ids=path, **kwargs)
                out.write(json.dumps(row) + "\n")
                seq += 1

            # No future object is preloaded. Request, then publish its actual objects.
            for request_id in range(args.requests):
                sid = rng.randrange(args.sessions)
                path = paths[sid]
                if rng.random() < 0.3:
                    path = path + [next_hash]
                    next_hash += 1
                    paths[sid] = path
                now = request_id * 10**9
                emit("request", path, now, request_id=request_id)
                for k in range(1, len(path) + 1):
                    emit("publish", path[:k], now, components="full")
                emit("publish", path, now, components="linear")
        files[str(seed)] = {"path": dest.name,
                            "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()}
    manifest = {"requests": args.requests, "sessions": args.sessions, "seeds": args.seeds,
                    "files": files, "source": "synthetic fixed backend demand and supply",
                    "full_capacity_bytes": (args.requests + args.sessions + 2) * 8,
                    "rule": "request exact current endpoint; publish FULL prefix and endpoint afterwards"}
    (args.output / "input.json").write_text(json.dumps(manifest, indent=2) + "\n")


def read_events(path):
    with path.open() as source:
        for line in source:
            yield HybridEvent(**json.loads(line))


def run(args):
    manifest = json.loads((args.output / "input.json").read_text())
    provenance = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                  text=True).strip(),
                      "diff": subprocess.check_output(["git", "diff"], cwd=ROOT, text=True),
                      "untracked": subprocess.check_output(
                          ["git", "ls-files", "--others", "--exclude-standard"],
                          cwd=ROOT, text=True).splitlines(), "input": manifest}
    # Hash all source inputs, including not-yet-committed implementation files.
    provenance["source_sha256"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for base in [ROOT / "cpp", ROOT / "src", ROOT / "benchmark"]
        for p in base.rglob("*") if p.suffix in {".cpp", ".hpp", ".py"}
    }
    (args.output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    results = []
    for seed, source in manifest["files"].items():
        src = args.output / source["path"]
        if hashlib.sha256(src.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("input SHA256 changed")
        for slots in args.slots:
            for policy in ["checkpoint_lru", "frequency", "benefit"]:
                h = HybridAttentionConfig(
                    enabled=True, full_capacity_bytes=manifest["full_capacity_bytes"],
                    linear_capacity_bytes=slots * 16, full_block_bytes=8,
                    linear_checkpoint_bytes=16, linear_policy=policy, alpha=args.alpha,
                )
                cfg = SimulationConfig(
                    block_size_bytes=8,
                    memory=MemoryConfig(h.full_capacity_bytes + h.linear_capacity_bytes),
                    slc=StorageTierConfig(8, 1), tlc=StorageTierConfig(8, 1),
                    storage_policy=StoragePolicyConfig(kind="infinite_storage"),
                    hybrid_attention=h,
                )
                path = args.output / f"seed{seed}-slots{slots}-{policy}"
                start = time.perf_counter()
                with DWPDSimulator(cfg, path) as sim:
                    sim.run_hybrid(read_events(src))
                results.append({"seed": int(seed), "slots": slots, "policy": policy,
                                    "elapsed_s": time.perf_counter() - start,
                                    "stats": sim.hybrid_stats()})
    (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")


def report(args):
    rows = json.loads((args.output / "results.json").read_text())
    with (args.output / "summary.csv").open("w") as out:
        writer = csv.writer(out)
        writer.writerow(["seed", "slots", "policy", "linear_reads", "linear_read_bytes",
                         "linear_write_bytes", "joint_available", "requests", "elapsed_s"])
        for r in rows:
            s = r["stats"]
            writer.writerow([r["seed"], r["slots"], r["policy"], s["linear"]["read_objects"],
                             s["linear"]["read_bytes"], s["linear"]["write_bytes"],
                             s["joint_available"], s["requests"], r["elapsed_s"]])
    print(f"{len(rows)} runs; logical object I/O summary: {args.output / 'summary.csv'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "run", "report"])
    parser.add_argument("--output", type=Path, default=ROOT / "build/hybrid-memory")
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--sessions", type=int, default=16)
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--slots", type=int, nargs="+", default=[8, 32, 128])
    parser.add_argument("--alpha", type=float, default=1.0)
    args = parser.parse_args()
    if min(args.requests, args.sessions, args.seeds, *args.slots) <= 0:
        parser.error("counts and capacities must be positive")
    {"prepare": prepare, "run": run, "report": report}[args.stage](args)


if __name__ == "__main__":
    main()
