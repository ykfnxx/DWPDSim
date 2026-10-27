"""Reproducible Kimi-K3 LRU-K replay throughput, with no trace or result lists.

Run from the repository root after rebuilding the native extension:
    .venv/bin/python benchmark/kimi_no_trace.py --requests 10000 --rounds 3
    .venv/bin/python benchmark/kimi_no_trace.py --baseline-package build/perf-before --cpu 21

Each round starts a fresh process and empty cache. The timer includes pybind batch
marshalling and native aggregate replay, with no trace or per-request result list.
Input generation, initial request construction, stats and serialization are outside
the timer. RSS includes inputs and metadata, not allocated KV payload. Comparison
mode warms each version once, then alternates baseline/current measured workers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import resource
import shutil
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import time
from array import array
from dataclasses import asdict, replace
from pathlib import Path

import dwpdsim
from dwpdsim import (
    DWPDSimulator,
    HybridPrefillRequest,
    MemoryConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
    _core,
    kimi_k3_profile,
)

ROOT = Path(__file__).resolve().parents[1]
METRICS = (
    "seconds", "requests_per_second", "token_blocks_per_second",
    "input_tokens_per_second", "peak_rss_bytes",
)


def configuration(tokens_per_block: int, k: int) -> SimulationConfig:
    profile = kimi_k3_profile(tokens_per_block=tokens_per_block)
    full_capacity = 512 * profile.full_block_bytes
    linear_capacity = 16 * profile.linear_checkpoint_bytes
    return SimulationConfig(
        memory=MemoryConfig(full_capacity + linear_capacity),
        slc=StorageTierConfig(1 << 20, 1),
        tlc=StorageTierConfig(1 << 20, 1),
        block_size_bytes=1 << 20,
        storage_policy=StoragePolicyConfig(kind="infinite_storage"),
        hybrid_attention=replace(profile.hybrid_config(
            lru_k=k,
            full_capacity_bytes=full_capacity,
            linear_capacity_bytes=linear_capacity,
            emit_io_trace=False,
        ), pool_allocation="fixed"),
    )


def workload(request_count: int, tokens_per_block: int, seed: int):
    """Shared prefix, 16 hot/48 cold sessions, and continued two-block growth."""
    rng = random.Random(seed)
    paths, visits = [], [0] * 64
    next_hash = 9
    for session in range(64):
        count = 24 + (session % 8) * 8
        paths.append(tuple(range(1, 9)) + tuple(range(next_hash, next_hash + count)))
        next_hash += count
    requests = []
    digest = hashlib.sha256()
    token_blocks = 0
    for sequence in range(request_count):
        session = rng.randrange(16) if sequence % 4 else rng.randrange(16, 64)
        visits[session] += 1
        if visits[session] % 4 == 0:
            paths[session] += (next_hash, next_hash + 1)
            next_hash += 2
        path = paths[session]
        requests.append(HybridPrefillRequest(
            timestamp_ns=sequence * 1_000_000,
            sequence=sequence,
            request_id=sequence,
            hash_ids=path,
            input_tokens=len(path) * tokens_per_block + 1,
        ))
        digest.update(array("Q", (sequence, len(path), *path)).tobytes())
        token_blocks += len(path)
    return requests, token_blocks, digest.hexdigest()


def worker(args) -> dict:
    if args.cpu is not None:
        os.sched_setaffinity(0, {args.cpu})
    requests, token_blocks, input_sha = workload(args.requests, args.tokens_per_block, args.seed)
    cfg = configuration(args.tokens_per_block, args.k)
    with tempfile.TemporaryDirectory(prefix="dwpdsim-kimi-no-trace-") as directory:
        trace_path = Path(directory) / "replay"
        with DWPDSimulator(cfg, trace_path) as sim:
            # Initial construction is outside; pybind copies each batch inside the timer.
            batches = [[sim._prefill_request(q) for q in requests[start:start + args.batch_size]]
                       for start in range(0, len(requests), args.batch_size)]
            start = time.perf_counter()
            for batch in batches:
                sim._core.run_hybrid_prefill_requests(batch)
            elapsed = time.perf_counter() - start
            stats = sim.hybrid_stats()
        assert not list(Path(directory).iterdir()), "no-trace replay created output files"
    assert stats["requests"] == args.requests
    assert stats["full"]["evictions"] > 0 and stats["linear"]["evictions"] > 0
    assert stats["full"]["write_bytes"] > 0 and stats["linear"]["write_bytes"] > 0
    assert stats["full"]["read_bytes"] > 0 and stats["linear"]["read_bytes"] > 0
    prefill = stats["prefill"]
    assert 0 < prefill["restored_tokens"] < prefill["input_tokens"]
    assert prefill["prefill_compute_tokens"] > args.requests
    return {
        "seconds": elapsed,
        "requests_per_second": args.requests / elapsed,
        "token_blocks_per_second": token_blocks / elapsed,
        "input_tokens_per_second": prefill["input_tokens"] / elapsed,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "request_count": args.requests,
        "input_token_blocks": token_blocks,
        "input_sha256": input_sha,
        "native_extension_sha256": hashlib.sha256(Path(_core.__file__).read_bytes()).hexdigest(),
        "native_extension_path": str(Path(_core.__file__).resolve()),
        "package_path": str(Path(dwpdsim.__file__).resolve().parent),
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "python_version": sys.version,
        "config": asdict(cfg),
        "stats": stats,
    }


def snapshot_current_package() -> Path:
    """Copy only current Python sources and the built extension into the benchmark snapshot."""
    package_root = ROOT / "build/perf-current"
    target = package_root / "dwpdsim"
    for source in (ROOT / "src/dwpdsim").rglob("*.py"):
        destination = target / source.relative_to(ROOT / "src/dwpdsim")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    native = Path(_core.__file__)
    shutil.copy2(native, target / native.name)
    return package_root


def run_worker(args, package_root: Path | None = None) -> dict:
    command = [sys.executable]
    environment = os.environ.copy()
    if package_root is not None:
        # -S suppresses editable .pth finders, which otherwise override PYTHONPATH.
        command.append("-S")
        package_paths = [str(package_root), sysconfig.get_path("purelib"),
                         sysconfig.get_path("platlib")]
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(package_paths))
    command += [str(Path(__file__).resolve()), "--worker",
                "--requests", str(args.requests), "--tokens-per-block", str(args.tokens_per_block),
                "--k", str(args.k), "--batch-size", str(args.batch_size), "--seed", str(args.seed)]
    if args.cpu is not None:
        command += ["--cpu", str(args.cpu)]
    completed = subprocess.run(
        command, check=True, stdout=subprocess.PIPE, text=True, cwd=ROOT, env=environment,
    )
    result = json.loads(completed.stdout)
    if package_root is not None:
        expected = package_root.resolve() / "dwpdsim"
        assert Path(result["package_path"]) == expected, "worker loaded another Python package"
        assert Path(result["native_extension_path"]).parent == expected, (
            "worker loaded another native extension"
        )
    return result


def medians(results: list[dict]) -> dict:
    return {key: statistics.median(row[key] for row in results) for key in METRICS}


def compare_packages(args) -> dict:
    baseline = args.baseline_package.resolve()
    current = snapshot_current_package()
    packages = {"before": baseline, "current": current}
    warmups = {name: run_worker(args, package) for name, package in packages.items()}
    measured = {name: [] for name in packages}
    order = []
    for round_index in range(args.rounds):
        for name, package in packages.items():
            measured[name].append(run_worker(args, package))
            order.append({"round": round_index + 1, "variant": name})
    all_results = [*warmups.values(), *(row for rows in measured.values() for row in rows)]
    input_equal = all(row["input_sha256"] == all_results[0]["input_sha256"] for row in all_results)
    stats_equal = all(row["stats"] == all_results[0]["stats"] for row in all_results)
    assert input_equal, "A/B replay inputs differ"
    assert stats_equal, "A/B replay produced different complete statistics"
    variants = {
        name: {"package_root": str(packages[name]), "warmup": warmups[name],
               "median": medians(measured[name]), "rounds": measured[name]}
        for name in packages
    }
    return {
        "warmups_per_variant": 1,
        "measurement_order": order,
        "input_sha_equal": input_equal,
        "complete_stats_equal": stats_equal,
        "variants": variants,
        "speedup": variants["before"]["median"]["seconds"]
        / variants["current"]["median"]["seconds"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=10_000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--tokens-per-block", type=int, default=16)
    parser.add_argument("--k", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=20261026)
    parser.add_argument("--baseline-package", type=Path,
                        help="package root containing the saved baseline dwpdsim directory")
    parser.add_argument("--cpu", type=int, help="pin each worker to this logical CPU")
    parser.add_argument("--output", type=Path, default=ROOT / "build/benchmark/kimi-no-trace.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rounds < 3 or min(args.requests, args.tokens_per_block, args.k, args.batch_size) <= 0:
        parser.error("rounds must be at least 3 and all workload sizes and K must be positive")
    if args.cpu is not None and args.cpu not in os.sched_getaffinity(0):
        parser.error("cpu must be in the calling process's allowed affinity set")
    if args.baseline_package is not None:
        if not (args.baseline_package / "dwpdsim/__init__.py").is_file():
            parser.error("baseline-package must contain dwpdsim/__init__.py")
        if args.baseline_package.resolve() == (ROOT / "build/perf-current").resolve():
            parser.error("baseline-package must differ from the generated current snapshot")
    if args.worker:
        print(json.dumps(worker(args)))
        return

    report = {
        "model": "Kimi-K3",
        "model_config_revision": kimi_k3_profile(tokens_per_block=args.tokens_per_block).config_revision,
        "cache_implementation_revision": kimi_k3_profile(
            tokens_per_block=args.tokens_per_block).cache_implementation_revision,
        "payload_scope": "whole-model logical FULL and LINEAR payload; no TP replication",
        "timing_scope": "pybind batch marshalling and native aggregate replay; no trace or result lists",
        "excluded_from_timing": "input generation, initial request construction, stats, serialization",
        "rss_scope": "fresh worker process including prebuilt inputs and metadata; no KV payload",
        "cache_start": "empty in every independent round",
        "storage_scope": "infinite backing store; 1 MiB address allocation unit; no SSD timing",
        "workload": {"seed": args.seed, "sessions": 64, "hot_sessions": 16,
                     "hot_request_fraction": 0.75, "growth_blocks_every_four_visits": 2,
                     "tokens_per_block": args.tokens_per_block, "lru_k": args.k,
                     "batch_size": args.batch_size},
        "round_count": args.rounds,
        "cpu": args.cpu,
    }
    if args.baseline_package is not None:
        report["comparison"] = compare_packages(args)
        summary = {"medians": {name: result["median"] for name, result in
                               report["comparison"]["variants"].items()},
                   "speedup": report["comparison"]["speedup"],
                   "complete_stats_equal": report["comparison"]["complete_stats_equal"]}
    else:
        results = [run_worker(args) for _ in range(args.rounds)]
        assert all(row["input_sha256"] == results[0]["input_sha256"] for row in results)
        assert all(row["stats"] == results[0]["stats"] for row in results)
        report.update(median=medians(results), rounds=results)
        summary = {"median": report["median"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), **summary}, indent=2))


if __name__ == "__main__":
    main()
