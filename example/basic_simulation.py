"""Run a small KV cache simulation and write trace plus metrics."""

from pathlib import Path

from dwpdsim import (
    DWPDSimulator,
    MemoryConfig,
    Request,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)

MIB = 1024 * 1024
BLOCK_SIZE = 4096


def main() -> None:
    config = SimulationConfig(
        block_size_bytes=BLOCK_SIZE,
        memory=MemoryConfig(capacity_bytes=2 * BLOCK_SIZE),
        slc=StorageTierConfig(capacity_bytes=MIB, stream_count=2),
        tlc=StorageTierConfig(capacity_bytes=MIB, stream_count=2),
        storage_policy=StoragePolicyConfig(
            kind="baseline_fixed_lru",
            fixed_tier="tlc",
            fixed_stream_id=0,
        ),
    )

    trace_path = Path("simulation_trace.csv")
    metrics_path = Path("simulation_metrics.json")
    with DWPDSimulator(config, trace_path) as simulator:
        simulator.run(
            [
                Request(0, 1, 10, [1, 2, 3]),
                Request(1_000_000_000, 2, 10, [1, 2, 4]),
                Request(2_000_000_000, 3, 20, [5, 6]),
            ]
        )

    simulator.write_stats(metrics_path)
    print(simulator.stats()["accesses"])
    print(f"trace written to {trace_path}")
    print(f"metrics written to {metrics_path}")


if __name__ == "__main__":
    main()
