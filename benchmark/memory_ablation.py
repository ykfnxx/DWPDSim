"""Reproducible Memory-only ablations; each measurement runs in a fresh process.

Generate a canonical Parquet workload, then compare boundary batching, prefetch,
indexing, grouping, real worker threads and sampled groups independently.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from dwpdsim import (
    DWPDSimulator,
    InputConfig,
    MemoryConfig,
    MemoryPolicyConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
    parquet_batches,
    replay_batches,
)

VARIANTS = {
    "rows": ("baseline_lru", 1, 0, 1, False),
    "batch": ("baseline_lru", 1, 0, 1, False),
    "queue": ("baseline_lru", 1, 0, 1, True),
    "context": ("context_lru", 1, 0, 1, False),
    "index": ("indexed_lru", 1, 0, 1, False),
    "groups": ("indexed_lru", 32, 0, 1, False),
    "workers": ("indexed_lru", 32, 0, 4, False),
    "sample": ("indexed_lru", 32, 4, 1, False),
    "sample_workers": ("indexed_lru", 32, 4, 4, False),
    "index_queue": ("indexed_lru", 1, 0, 1, True),
}


def dependencies():
    return {name: importlib.metadata.version(name) for name in ("numpy", "pyarrow", "dwpdsim")}


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def generate(path, count, seed, length):
    rng = random.Random(seed)
    offsets = [0]
    hashes = []
    for i in range(count):
        root = 512 + i if i % 7 == 0 else rng.randrange(64 if rng.random() < 0.7 else 512)
        branch = rng.randrange(16)
        prefix = min(4, length)
        hashes.extend(root * 100_000 + j for j in range(prefix))
        hashes.extend(root * 100_000 + 100 + branch * length + j for j in range(length - prefix))
        offsets.append(len(hashes))
    table = pa.table(
        {
            "timestamp_ns": pa.array(np.arange(count, dtype=np.uint64) * 1000),
            "request_id": pa.array(np.arange(count, dtype=np.uint64)),
            "affinity_id": pa.array(np.arange(count, dtype=np.uint64) % 64),
            "hash_ids": pa.ListArray.from_arrays(
                pa.array(offsets, type=pa.int32()), pa.array(hashes, type=pa.uint64())
            ),
        }
    )
    pq.write_table(table, path, row_group_size=1024)


def measure(args):
    kind, groups, sample, workers, prefetch = VARIANTS[args.variant]
    block = 8 * 1024 * 1024
    cfg = SimulationConfig(
        memory=MemoryConfig(args.memory_blocks * block),
        slc=StorageTierConfig(args.slc_blocks * block, 1),
        tlc=StorageTierConfig(args.tlc_blocks * block, 1),
        block_size_bytes=block,
        memory_policy=MemoryPolicyConfig(
            kind=kind,
            groups=groups,
            sampled_groups=sample,
            workers=workers,
            seed=args.seed,
            profile=args.profile,
            alpha=args.alpha,
            retention_ns=args.retention_ns,
            max_eviction_blocks=args.max_eviction_blocks if kind == "context_lru" else None,
            retention_growth_seconds_per_block=(args.retention_growth_seconds_per_block
                                                if kind == "context_lru" else 0),
            eviction_gap_reference_ns=(args.eviction_gap_reference_ns
                                       if kind == "context_lru" else None),
            eviction_base_blocks=args.eviction_base_blocks,
        ),
        storage_policy=StoragePolicyConfig(kind=args.storage_policy),
    )
    input_cfg = InputConfig(
        batch_requests=args.batch_requests, batch_hashes=262_144, prefetch=prefetch
    )
    trace = args.output.with_suffix(".trace.csv")
    start = time.perf_counter()
    sim = DWPDSimulator(cfg, trace)
    initialized = time.perf_counter()
    windows = []

    class Consumer:
        def process_batch(self, *buffers):
            before = time.perf_counter()
            if args.variant == "rows":
                timestamps, ids, affinity, offsets, hashes = buffers
                for i in range(len(timestamps)):
                    sim.process(
                        int(timestamps[i]),
                        int(ids[i]),
                        int(affinity[i]),
                        hashes[int(offsets[i]) : int(offsets[i + 1])].tolist(),
                    )
            else:
                sim.process_batch(*buffers)
            elapsed = time.perf_counter() - before
            stats = sim.stats()
            windows.append(
                {
                    "requests": stats["accesses"]["requests"],
                    "accesses": stats["accesses"]["total"],
                    "events": stats["trace"]["events"],
                    "batch_replay_s": elapsed,
                    "nodes": sim.node_count,
                    "memory": sim.memory_performance(),
                }
            )

        def finish(self):
            sim.finish()

    inputs = replay_batches(Consumer(), parquet_batches([args.dataset], input_cfg), input_cfg)
    elapsed = time.perf_counter() - initialized
    stats = sim.stats()
    result = {
        "dependencies": dependencies(),
        "variant": args.variant,
        "profile": args.profile,
        "config": asdict(cfg),
        "input_config": asdict(input_cfg),
        "init_s": initialized - start,
        "elapsed_s": elapsed,
        "input": inputs,
        "memory": sim.memory_performance(),
        "stats": stats,
        "windows": windows,
        "peak_rss_mib": float(
            next(
                line.split()[1]
                for line in Path("/proc/self/status").read_text().splitlines()
                if line.startswith("VmHWM:")
            )
        )
        / 1024,
        "peak_rss_source": "/proc/self/status VmHWM",
        "rusage_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "trace_bytes": trace.stat().st_size if trace.exists() else 0,
        "trace_sha256": digest(trace) if trace.exists() else None,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if trace.exists():
        trace.unlink()


def suite(args):
    args.output.mkdir(parents=True, exist_ok=True)
    if args.dataset is None:
        args.dataset = args.output / "workload.parquet"
        generate(args.dataset, args.requests, args.seed, args.path_length)
    variants = args.variants.split(",")
    provenance = {
        "command": sys.argv,
        "dataset": str(args.dataset.resolve()),
        "dataset_sha256": digest(args.dataset),
        "python": sys.version,
        "dependencies": dependencies(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    source_files = [Path("CMakeLists.txt"), Path("pyproject.toml")]
    for root in ("cpp", "src/dwpdsim", "benchmark"):
        source_files.extend(p for p in Path(root).rglob("*") if p.suffix in (".cpp", ".hpp", ".py"))
    provenance["source_sha256"] = {str(p): digest(p) for p in sorted(source_files)}
    (args.output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    jobs = [(variant, rep) for rep in range(args.repeats) for variant in variants]
    random.Random(args.seed).shuffle(jobs)
    results = {variant: [] for variant in variants}
    for variant, rep in jobs:
        target = args.output / f"{variant}-{rep}.json"
        command = [
            sys.executable,
            __file__,
            "--measure",
            "--variant",
            variant,
            "--dataset",
            str(args.dataset),
            "--output",
            str(target),
            "--memory-blocks",
            str(args.memory_blocks),
            "--slc-blocks",
            str(args.slc_blocks),
            "--tlc-blocks",
            str(args.tlc_blocks),
            "--batch-requests",
            str(args.batch_requests),
            "--seed",
            str(args.seed),
            "--storage-policy",
            args.storage_policy,
            "--alpha",
            str(args.alpha),
        ]
        command.extend(["--retention-growth-seconds-per-block", str(args.retention_growth_seconds_per_block),
                        "--eviction-base-blocks", str(args.eviction_base_blocks)])
        if args.eviction_gap_reference_ns is not None:
            command.extend(["--eviction-gap-reference-ns", str(args.eviction_gap_reference_ns)])
        if args.max_eviction_blocks is not None:
            command.extend(["--max-eviction-blocks", str(args.max_eviction_blocks)])
        if args.retention_ns is not None:
            command.extend(["--retention-ns", str(args.retention_ns)])
        if args.profile:
            command.append("--profile")
        subprocess.run(command, check=True)
        result = json.loads(target.read_text())
        results[variant].append(result)
        print(
            f"{variant} rep={rep} elapsed={result['elapsed_s']:.4f}s "
            f"rss={result['peak_rss_mib']:.1f}MiB",
            flush=True,
        )
    summary = {}
    for variant, entries in results.items():
        elapsed = [r["elapsed_s"] for r in entries]
        summary[variant] = {
            "median_s": statistics.median(elapsed),
            "min_s": min(elapsed),
            "max_s": max(elapsed),
            "median_rss_mib": statistics.median(r["peak_rss_mib"] for r in entries),
            "trace_sha256": entries[0]["trace_sha256"],
            "stats": entries[0]["stats"],
            "memory": entries[0]["memory"],
        }
        if any(r["stats"] != entries[0]["stats"] for r in entries):
            raise AssertionError(f"{variant} metrics are not deterministic")
        if len({r["trace_sha256"] for r in entries}) != 1:
            raise AssertionError(f"{variant} is not deterministic")
    exact = [v for v in variants if not v.startswith("sample") and v != "context"]
    if exact:
        reference = summary[exact[0]]
        for variant in exact:
            assert summary[variant]["trace_sha256"] == reference["trace_sha256"], variant
            assert summary[variant]["stats"] == reference["stats"], variant
    if "sample" in summary and "sample_workers" in summary:
        assert summary["sample"]["trace_sha256"] == summary["sample_workers"]["trace_sha256"]
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {v: {k: x for k, x in r.items() if k.endswith("_s")} for v, r in summary.items()},
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests", type=int, default=20_000)
    parser.add_argument("--path-length", type=int, default=16)
    parser.add_argument("--memory-blocks", type=int, default=4096)
    parser.add_argument("--slc-blocks", type=int, default=1_048_576)
    parser.add_argument("--tlc-blocks", type=int, default=7_340_032)
    parser.add_argument("--batch-requests", type=int, default=1024)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--variant", choices=VARIANTS, default="batch")
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--storage-policy", choices=["baseline_fixed_lru", "infinite_storage"],
                        default="baseline_fixed_lru")
    parser.add_argument("--alpha", type=float, default=0.01)
    parser.add_argument("--retention-ns", type=int)
    parser.add_argument("--max-eviction-blocks", type=int,
                        help="context_lru only: maximum resident blocks evicted per decision")
    parser.add_argument("--retention-growth-seconds-per-block", type=float, default=0.0)
    parser.add_argument("--eviction-gap-reference-ns", type=int)
    parser.add_argument("--eviction-base-blocks", type=int, default=64)
    args = parser.parse_args()
    if args.measure:
        measure(args)
    else:
        suite(args)


if __name__ == "__main__":
    main()
