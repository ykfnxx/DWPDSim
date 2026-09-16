"""Measure storage-policy replay cost on a fixed cyclic workload.

Policies may produce different I/O; this is workload cost, not equal-work speedup.
Generated traces and metrics are stored under --output.
"""

import argparse
import json
import time
from pathlib import Path

from dwpdsim import (
    DWPDSimulator,
    MemoryConfig,
    MemoryPolicyConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=20000)
    parser.add_argument("--output", type=Path, default=Path("build/shadow-overhead"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for kind, online in [
        ("adaptive_endurance", False),
        ("wear_balanced", False),
        ("wear_balanced", True),
    ]:
        config = SimulationConfig(
            block_size_bytes=4096,
            memory=MemoryConfig(32 * 4096),
            slc=StorageTierConfig(128 * 4096, 2),
            tlc=StorageTierConfig(256 * 4096, 2),
            memory_policy=MemoryPolicyConfig(kind="context_lru"),
            storage_policy=StoragePolicyConfig(
                kind=kind,
                online_tuning=online,
                shadow_pages_per_block=16,
                # Tiny pools need room for multiple open stream frontiers and GC.
                shadow_overprovisioning=0.25,
                min_reuse_blocks=64,
                reuse_ema_scale_blocks=512,
            ),
        )
        name = kind + ("-online" if online else "")
        started = time.perf_counter()
        with DWPDSimulator(config, args.output / (name + ".csv")) as sim:
            for i in range(args.requests):
                sim.process(i * 1_000_000_000, i, 1 + i % 16, [1 + i % 1024])
        elapsed = time.perf_counter() - started
        stats = sim.stats()
        sim.write_stats(args.output / (name + ".json"))
        results.append({
            "kind": name,
            "requests": args.requests,
            "seconds": elapsed,
            "trace_events": stats["trace"]["events"],
            "feedback_windows": stats["algorithm"].get("feedback_windows", 0),
        })
    text = json.dumps(results, indent=2) + "\n"
    (args.output / "results.json").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
