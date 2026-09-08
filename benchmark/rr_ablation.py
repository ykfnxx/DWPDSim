"""Exact RR ablations with fresh processes, trace equality and per-batch windows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def measure(args):
    if args.variant == "original":
        from importlib.machinery import PathFinder

        # Prefer the baseline wheel over the active checkout's editable import hook.
        sys.path.insert(0, str(args.original_package.resolve()))
        sys.meta_path.insert(0, PathFinder)
    from dwpdsim import (
        DWPDSimulator,
        InputConfig,
        MemoryConfig,
        MemoryPolicyConfig,
        SimulationConfig,
        StoragePolicyConfig,
        StorageTierConfig,
        _core,
        parquet_batches,
    )

    if args.variant == "original":
        assert Path(_core.__file__).resolve().is_relative_to(args.original_package.resolve()), (
            "original measurement must load the independently built baseline package"
        )

    options = (
        {}
        if args.variant == "original"
        else {
            "rr_victim_search": "indexed" if args.variant in ("index", "counts") else args.variant,
            "rr_subtree_counts": args.variant == "counts",
            "rr_verify_victims": args.verify,
            "rr_profile": args.profile,
        }
    )
    block = 8 * 1024 * 1024
    cfg = SimulationConfig(
        memory=MemoryConfig(args.memory_blocks * block),
        slc=StorageTierConfig(args.slc_blocks * block, 3),
        tlc=StorageTierConfig(args.tlc_blocks * block, 2),
        block_size_bytes=block,
        memory_policy=MemoryPolicyConfig(kind="indexed_lru", admit_storage_hits=not args.bypass),
        storage_policy=StoragePolicyConfig(kind="wear_share_round_robin", **options),
    )
    inputs = InputConfig(batch_requests=512, batch_hashes=262144, prefetch=False)
    trace = args.output.with_suffix(".csv")
    sim = DWPDSimulator(cfg, trace)
    windows = []
    start = time.perf_counter()
    for batch in parquet_batches([args.dataset], inputs):
        before = time.perf_counter()
        sim.process_batch(*batch.buffers)
        windows.append(
            {
                "replay_s": time.perf_counter() - before,
                "requests": sim.stats()["accesses"]["requests"],
                "events": sim.stats()["trace"]["events"],
                "storage": {} if args.variant == "original" else sim.storage_performance(),
            }
        )
    sim.finish()
    elapsed = time.perf_counter() - start
    result = {
        "variant": args.variant,
        "profile": args.profile,
        "verify": args.verify,
        "config": asdict(cfg),
        "elapsed_s": elapsed,
        "windows": windows,
        "stats": sim.stats(),
        "trace_sha256": digest(trace),
        "trace_bytes": trace.stat().st_size,
        "storage": {} if args.variant == "original" else sim.storage_performance(),
        "core_path": _core.__file__,
        "core_sha256": digest(_core.__file__),
        "peak_rss_mib": int(
            next(
                line.split()[1]
                for line in Path("/proc/self/status").read_text().splitlines()
                if line.startswith("VmHWM:")
            )
        )
        / 1024,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    trace.unlink()


def suite(args):
    args.output.mkdir(parents=True, exist_ok=True)
    if args.dataset is None:
        from memory_ablation import generate

        args.dataset = args.output / "workload.parquet"
        generate(args.dataset, args.requests, args.seed, args.path_length)
    source_files = [Path("CMakeLists.txt"), Path("pyproject.toml")]
    for root in ("cpp", "src/dwpdsim", "benchmark"):
        source_files.extend(p for p in Path(root).rglob("*") if p.suffix in (".cpp", ".hpp", ".py"))
    provenance = {
        "command": sys.argv,
        "dataset": str(args.dataset.resolve()),
        "dataset_sha256": digest(args.dataset),
        "python": sys.version,
        "platform": platform.platform(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_sha256": {str(p): digest(p) for p in sorted(source_files)},
    }
    (args.output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    variants = args.variants.split(",")
    jobs = [(v, r) for v in variants for r in range(args.repeats)]
    random.Random(args.seed).shuffle(jobs)
    results = {v: [] for v in variants}
    reference = None
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
        ]
        for option in ("memory_blocks", "slc_blocks", "tlc_blocks", "original_package"):
            value = getattr(args, option)
            if value is not None:
                command.extend(["--" + option.replace("_", "-"), str(value)])
        for option in ("profile", "verify", "bypass"):
            if getattr(args, option):
                command.append("--" + option)
        subprocess.run(command, check=True)
        result = json.loads(target.read_text())
        signature = (result["stats"], result["trace_sha256"])
        if reference is None:
            reference = signature
        assert signature == reference, f"metrics/trace mismatch: {variant} repetition {rep}"
        results[variant].append(result)
        print(f"{variant} rep={rep}: {result['elapsed_s']:.4f}s", flush=True)
    summary = {}
    for variant, runs in results.items():
        times = [run["elapsed_s"] for run in runs]
        summary[variant] = {
            "median_s": statistics.median(times),
            "min_s": min(times),
            "max_s": max(times),
            "median_rss_mib": statistics.median(r["peak_rss_mib"] for r in runs),
            "storage": runs[0]["storage"],
            "stats": runs[0]["stats"],
            "trace_sha256": runs[0]["trace_sha256"],
        }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({v: round(r["median_s"], 4) for v, r in summary.items()}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--requests", type=int, default=6000)
    parser.add_argument("--path-length", type=int, default=16)
    parser.add_argument("--memory-blocks", type=int, default=256)
    parser.add_argument("--slc-blocks", type=int, default=1024)
    parser.add_argument("--tlc-blocks", type=int, default=2048)
    parser.add_argument("--original-package", type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--variants", default="scan,fused,index,counts")
    parser.add_argument("--variant", choices=("original", "scan", "fused", "index", "counts"))
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--bypass", action="store_true", help="Do not admit storage hits to Memory")
    args = parser.parse_args()
    if (
        "original" in args.variants.split(",") or args.variant == "original"
    ) and args.original_package is None:
        parser.error("original requires --original-package built from the baseline commit")
    if args.measure:
        measure(args)
    else:
        suite(args)


if __name__ == "__main__":
    main()
