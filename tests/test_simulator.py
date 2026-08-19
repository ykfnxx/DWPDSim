import pytest

from dwpdsim import DWPDSimulator, Query, SimulationConfig, StorageTier, TierConfig
from dwpdsim.errors import OutOfOrderQueryError
from dwpdsim.models import IOOperation, IOReason
from dwpdsim.policies import AlwaysQLCPolicy


def make_config() -> SimulationConfig:
    return SimulationConfig(
        block_size_bytes=4096,
        dram=TierConfig(1),
        tlc=TierConfig(1),
        qlc=TierConfig(4),
    )


def test_simulator_reports_hit_rates_io_and_capacity() -> None:
    simulator = DWPDSimulator.from_config(
        make_config(),
        initial_blocks=(1, 2),
        storage_placement_policy=AlwaysQLCPolicy(),
    )

    report = simulator.run([Query(timestamp=10, block_ids=(1, 1, 2))])

    assert report.metrics.query_count == 1
    assert report.metrics.block_access_count == 3
    assert report.metrics.dram_hits == 1
    assert report.metrics.dram_misses == 2
    assert report.metrics.dram_hit_rate == pytest.approx(1 / 3)
    assert report.metrics.tlc_hit_rate_on_dram_miss == 0.0
    assert report.metrics.qlc_hit_rate_on_dram_miss == 1.0
    assert (
        report.metrics.io_operations(
            StorageTier.QLC,
            IOOperation.READ,
            IOReason.DEMAND,
        )
        == 2
    )
    assert (
        report.metrics.io_operations(
            StorageTier.QLC,
            IOOperation.WRITE,
            IOReason.WRITEBACK,
        )
        == 1
    )
    qlc_reads = next(
        count
        for count in report.metrics.io_counts
        if count.tier is StorageTier.QLC and count.operation is IOOperation.READ
    )
    assert qlc_reads.bytes == 8192
    assert report.dram.capacity_blocks == 1
    assert report.dram.used_blocks == 1
    assert report.dram.peak_used_blocks == 1
    assert report.dram.eviction_count == 1
    assert report.qlc.used_blocks == 2


def test_simulator_rejects_decreasing_timestamps() -> None:
    simulator = DWPDSimulator.from_config(make_config(), initial_blocks=(1,))
    simulator.process_query(Query(timestamp=10, block_ids=(1,)))

    with pytest.raises(OutOfOrderQueryError):
        simulator.process_query(Query(timestamp=9, block_ids=(1,)))


def test_aggregate_run_matches_detailed_query_processing() -> None:
    config = SimulationConfig(
        block_size_bytes=4096,
        dram=TierConfig(2),
        tlc=TierConfig(2),
        qlc=TierConfig(6),
    )
    queries = (
        Query(timestamp=10, block_ids=(1, 2, 1)),
        Query(timestamp=20, block_ids=(3, 4, 2)),
        Query(timestamp=30, block_ids=(5, 1, 6)),
    )
    detailed = DWPDSimulator.from_config(config, initial_blocks=range(1, 7))
    aggregate = DWPDSimulator.from_config(config, initial_blocks=range(1, 7))

    for query in queries:
        detailed.process_query(query)
    aggregate_report = aggregate.run(iter(queries))

    assert aggregate_report == detailed.report()
    assert aggregate.memory.resident_blocks == detailed.memory.resident_blocks
    assert aggregate.storage.blocks_in_tier(StorageTier.TLC) == detailed.storage.blocks_in_tier(
        StorageTier.TLC
    )
    assert aggregate.storage.blocks_in_tier(StorageTier.QLC) == detailed.storage.blocks_in_tier(
        StorageTier.QLC
    )


def test_run_does_not_build_detailed_query_results(monkeypatch: pytest.MonkeyPatch) -> None:
    simulator = DWPDSimulator.from_config(make_config(), initial_blocks=(1, 2))

    def fail_if_called(query: Query) -> None:
        raise AssertionError(f"detailed processing unexpectedly called for {query}")

    monkeypatch.setattr(simulator.memory, "process_query", fail_if_called)

    report = simulator.run(Query(timestamp=index, block_ids=(1,)) for index in range(3))

    assert report.metrics.query_count == 3
    assert report.metrics.block_access_count == 3
