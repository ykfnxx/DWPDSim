"""Run a small DRAM/TLC/QLC simulation and print its report."""

from dwpdsim import DWPDSimulator, Query, SimulationConfig, TierConfig
from dwpdsim.metrics import SimulationReport, TierUsage
from dwpdsim.policies import AlwaysTLCPolicy, LRUPolicy


def build_simulator() -> DWPDSimulator:
    """Create a simulator with deliberately small capacities to trigger evictions."""

    config = SimulationConfig(
        block_size_bytes=4096,
        dram=TierConfig(capacity_blocks=2),
        tlc=TierConfig(capacity_blocks=2),
        qlc=TierConfig(capacity_blocks=16),
    )
    return DWPDSimulator.from_config(
        config,
        initial_blocks=range(1, 7),
        memory_cache_policy=LRUPolicy(write_back_on_remove=True),
        storage_placement_policy=AlwaysTLCPolicy(),
        storage_cache_policy=LRUPolicy(write_back_on_remove=True),
    )


def build_queries() -> tuple[Query, ...]:
    """Create an ordered workload with both repeated and new block accesses."""

    return (
        Query(timestamp=10, block_ids=(1, 2, 1), query_id="query-1"),
        Query(timestamp=20, block_ids=(3, 1, 4), query_id="query-2"),
        Query(timestamp=30, block_ids=(2, 5, 6, 2), query_id="query-3"),
    )


def format_tier(name: str, usage: TierUsage) -> str:
    """Format one hierarchy tier's capacity summary."""

    return (
        f"  {name:<4} used={usage.used_blocks}/{usage.capacity_blocks}, "
        f"peak={usage.peak_used_blocks}, evictions={usage.eviction_count}"
    )


def print_report(report: SimulationReport) -> None:
    """Print hit rates, capacity usage, and physical I/O groups."""

    metrics = report.metrics
    print("DWPDSim simulation report")
    print(f"  queries={metrics.query_count}, block_accesses={metrics.block_access_count}")
    print(
        "  hit_rates: "
        f"DRAM={metrics.dram_hit_rate:.2%}, "
        f"TLC|DRAM-miss={metrics.tlc_hit_rate_on_dram_miss:.2%}, "
        f"QLC|DRAM-miss={metrics.qlc_hit_rate_on_dram_miss:.2%}"
    )

    print("Capacity")
    print(format_tier("DRAM", report.dram))
    print(format_tier("TLC", report.tlc))
    print(format_tier("QLC", report.qlc))

    print("Physical I/O")
    for io_count in metrics.io_counts:
        print(
            f"  {io_count.tier.value:<3} "
            f"{io_count.operation.value:<5} "
            f"reason={io_count.reason.value:<9} "
            f"operations={io_count.operations}, "
            f"blocks={io_count.blocks}, bytes={io_count.bytes}"
        )


def main() -> None:
    simulator = build_simulator()
    report = simulator.run(build_queries())
    print_report(report)


if __name__ == "__main__":
    main()
