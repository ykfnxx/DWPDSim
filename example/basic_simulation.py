"""Run a small KV cache simulation and write trace plus metrics."""

from pathlib import Path

from dwpdsim import (
    DWPDSimulator,
    PlacementPolicyConfig,
    Request,
    SimulationConfig,
    StorageTierConfig,
)

MIB = 1024 * 1024
BLOCK_SIZE = 8 * MIB


def main() -> None:
    config = SimulationConfig(
        block_size_bytes=BLOCK_SIZE,
        memory_capacity_bytes=2 * BLOCK_SIZE,
        slc=StorageTierConfig(capacity_bytes=4 * BLOCK_SIZE, stream_count=2),
        tlc=StorageTierConfig(capacity_bytes=8 * BLOCK_SIZE, stream_count=2),
        timestamp_unit="us",
    )

    trace_path = Path("simulation_trace.csv")
    metrics_path = Path("simulation_metrics.json")
    with DWPDSimulator(
        config,
        trace_path,
        placement_policy=PlacementPolicyConfig(
            kind="fixed",
            fixed_tier="tlc",
            fixed_stream_id=0,
        ),
    ) as simulator:
        simulator.run(
            [
                Request(timestamp=0, hash_ids=[1, 2, 3]),
                Request(timestamp=1_000, hash_ids=[1, 2, 4]),
                Request(timestamp=2_000, hash_ids=[5, 6]),
            ]
        )

    simulator.write_stats(metrics_path)
    print(simulator.stats()["accesses"])
    print(f"trace written to {trace_path}")
    print(f"metrics written to {metrics_path}")


if __name__ == "__main__":
    main()
