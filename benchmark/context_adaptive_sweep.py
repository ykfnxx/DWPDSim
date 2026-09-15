"""Run retention-only, grain-only, then combined sweeps on canonical Parquet input."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from dwpdsim import (
    DWPDSimulator,
    InputConfig,
    MemoryConfig,
    MemoryPolicyConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
    parquet_batches,
)


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def measure(args, job):
    memory = MemoryPolicyConfig(
        kind="context_lru", alpha=args.alpha, admit_storage_hits=False,
        retention_ns=None if job["retention_s"] is None else int(job["retention_s"] * 1e9),
        retention_growth_seconds_per_block=job["beta"],
        eviction_gap_reference_ns=None if job["gap_s"] is None else int(job["gap_s"] * 1e9),
        eviction_base_blocks=args.base_blocks,
        max_eviction_blocks=args.max_blocks if job["gap_s"] is not None else args.base_blocks,
    )
    cfg = SimulationConfig(
        memory=MemoryConfig(args.memory_blocks * 8 * 1024**2), block_size_bytes=8 * 1024**2,
        slc=StorageTierConfig(0, 0), tlc=StorageTierConfig(0, 0), memory_policy=memory,
        storage_policy=StoragePolicyConfig(kind="infinite_storage"),
    )
    start = time.perf_counter()
    with DWPDSimulator(cfg, args.output / "unused.csv") as sim:
        if args.diagnostics:
            sim.enable_memory_diagnostics(args.output / f"{job['name']}.evictions.csv")
        processed, checkpoint = 0, 10000
        for batch in parquet_batches([args.dataset], InputConfig(batch_requests=512)):
            sim.process_batch(*batch.buffers)
            processed += len(batch.timestamps_ns)
            if processed >= checkpoint:
                print(job["name"], processed, "requests", flush=True)
                checkpoint += 10000
    result = {"job": job, "config": asdict(cfg), "stats": sim.stats(),
                  "memory_work": sim.memory_performance(), "elapsed_s": time.perf_counter() - start}
    (args.output / f"{job['name']}.json").write_text(json.dumps(result, indent=2) + "\n")


def run_phase(args, jobs):
    def run(job):
        command = [sys.executable, __file__, "--dataset", str(args.dataset), "--output",
                   str(args.output), "--memory-blocks", str(args.memory_blocks), "--alpha",
                   str(args.alpha), "--base-blocks", str(args.base_blocks), "--max-blocks",
                   str(args.max_blocks), "--job", json.dumps(job)]
        if args.diagnostics:
            command.append("--diagnostics")
        with (args.output / f"{job['name']}.log").open("w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        result = json.loads((args.output / f"{job['name']}.json").read_text())
        access = result["stats"]["accesses"]
        print(job["name"], f"hit={100 * (1 - access['global_misses'] / access['total']):.4f}%",
              "drop=", result["stats"]["memory"]["drop_blocks"], flush=True)
        return result

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = [f.result() for f in as_completed([pool.submit(run, j) for j in jobs])]
    return results


def report(args, results):
    baseline = next(r for r in results if r["job"]["name"] == "baseline-off")["stats"]["accesses"]
    baseline_hit = 100 * (1 - baseline["global_misses"] / baseline["total"])
    baseline_writes = next(r for r in results if r["job"]["name"] == "baseline-off")["stats"]["storage"]["tlc"]["writes"]["blocks"]
    rows = []
    for result in sorted(results, key=lambda r: (r["job"]["stage"], r["job"]["retention_s"] or 0, r["job"]["beta"], r["job"]["gap_s"] or 0)):
        job, stats = result["job"], result["stats"]
        access = stats["accesses"]
        assert access["total"] == baseline["total"] and access["requests"] == baseline["requests"]
        rows.append(dict(
            **job, hit_pct=100 * (1 - access["global_misses"] / access["total"]),
            baseline_hit_pct=baseline_hit, drop_blocks=stats["memory"]["drop_blocks"],
            evicted_blocks=stats["memory"]["evicted_blocks"],
            drop_eviction_pct=(100 * stats["memory"]["drop_blocks"] / stats["memory"]["evicted_blocks"]
                               if stats["memory"]["evicted_blocks"] else 0.0),
            write_blocks=stats["storage"]["tlc"]["writes"]["blocks"],
            write_reduction_pct=(100 * (1 - stats["storage"]["tlc"]["writes"]["blocks"] / baseline_writes)
                                 if baseline_writes else None),
            memory_hit_pct=100 * access["memory_hit_rate"], compute_cost=access["compute_cost"],
        ))
    with (args.output / "results.csv").open("w") as target:
        writer = csv.DictWriter(target, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Adaptive context_lru sweep", "",
             f"Memory {args.memory_blocks} blocks, 8 MiB/block; alpha={args.alpha}; admission off.",
             f"Base retention={args.retentions_s or args.retention_s}s; base grain={args.base_blocks}. "
             + ("Dynamic grain disabled; fixed cap equals base grain." if args.retentions_s else
                f"Dynamic grain hard max={args.max_blocks}."),
             "Hit rate is the continuous-prefix Memory + Storage block hit rate. Each run starts cold.",
             "Stages run sequentially; jobs within a stage run concurrently. Timing is not a performance comparison.",
             ("Retention grid uses fixed grain and all retention/beta combinations." if args.retentions_s else
              "Combined stage uses the two best nonzero betas and two best gap references from prior stages."),
             "This is selection on one trace, not held-out validation or a complete parameter grid.", "",
             "| stage | retention s | beta s/block | gap reference s | hit % | baseline % | Drop blocks | Evicted blocks | Drop / evicted % | WRITE blocks | Write reduction % |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        reduction = "N/A" if row["write_reduction_pct"] is None else f"{row['write_reduction_pct']:.2f}"
        lines.append(f"| {row['stage']} | {row['retention_s']} | {row['beta']} | {row['gap_s']} | {row['hit_pct']:.4f} | "
                     f"{baseline_hit:.4f} | {row['drop_blocks']:,} | {row['evicted_blocks']:,} | "
                     f"{row['drop_eviction_pct']:.2f} | {row['write_blocks']:,} | "
                     f"{reduction} |")
    (args.output / "report.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-blocks", type=int, default=16384)
    parser.add_argument("--alpha", type=float, default=.01)
    parser.add_argument("--retentions-s", help="Comma-separated retention grid; skips grain stages")
    parser.add_argument("--retention-s", type=float, default=60)
    parser.add_argument("--base-blocks", type=int, default=64)
    parser.add_argument("--max-blocks", type=int, default=1024)
    parser.add_argument("--betas", default="0,1,2,5")
    parser.add_argument("--gap-references-s", default="15,30,60,120,300")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--diagnostics", action="store_true", help="Record block evictions for offline analysis")
    parser.add_argument("--job", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.job:
        measure(args, json.loads(args.job))
        return
    sources = [p for root in ("cpp", "src/dwpdsim") for p in Path(root).rglob("*")
               if p.suffix in (".cpp", ".hpp", ".py")]
    sources += [Path(__file__), Path("pyproject.toml"), Path("uv.lock")]
    (args.output / "provenance.json").write_text(json.dumps({
        "command": sys.argv, "dataset": str(args.dataset.resolve()), "dataset_sha256": digest(args.dataset),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "diff": subprocess.check_output(["git", "diff"], text=True),
        "sources": {str(p): digest(p) for p in sources},
    }, indent=2))

    def job(name, stage, beta=0, gap=None, retention=args.retention_s):
        return {"name": name, "stage": stage, "beta": beta, "gap_s": gap, "retention_s": retention}

    if args.retentions_s:
        grid = [job("baseline-off", "baseline", retention=None)]
        grid += [job(f"retention-{retention}-beta-{beta}", "retention-grid",
                     beta=beta, retention=retention)
                 for retention in map(float, args.retentions_s.split(","))
                 for beta in map(float, args.betas.split(","))]
        report(args, run_phase(args, grid))
        return

    first = [job("baseline-off", "baseline", retention=None)]
    first += [job(f"retention-{beta}", "1-retention", beta=beta)
              for beta in map(float, args.betas.split(","))]
    results = run_phase(args, first)
    report(args, results)
    second = [job(f"grain-{gap}", "2-grain", gap=gap)
              for gap in map(float, args.gap_references_s.split(","))]
    results += run_phase(args, second)
    report(args, results)

    def rank(result):
        return (result["stats"]["accesses"]["global_misses"],
                result["stats"]["memory"]["drop_blocks"], result["job"]["name"])

    betas = sorted([r for r in results if r["job"]["stage"] == "1-retention" and
                    r["job"]["beta"] > 0], key=rank)[:2]
    gaps = sorted([r for r in results if r["job"]["stage"] == "2-grain"], key=rank)[:2]
    third = [job(f"combined-{b['job']['beta']}-{g['job']['gap_s']}", "3-combined",
                 beta=b["job"]["beta"], gap=g["job"]["gap_s"]) for b in betas for g in gaps]
    results += run_phase(args, third)
    report(args, results)


if __name__ == "__main__":
    main()
