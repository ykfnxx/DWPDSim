import csv

import numpy as np
import pytest

from dwpdsim import (
    DWPDSimulator,
    MemoryConfig,
    MemoryPolicyConfig,
    Request,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)


def config(
    *,
    memory_blocks=1,
    slc_blocks=4,
    tlc_blocks=8,
    slc_streams=2,
    tlc_streams=2,
    memory_policy=None,
    storage_policy=None,
    simulation_end_ns=None,
):
    block_size = 512
    return SimulationConfig(
        block_size_bytes=block_size,
        memory=MemoryConfig(memory_blocks * block_size),
        slc=StorageTierConfig(slc_blocks * block_size, slc_streams),
        tlc=StorageTierConfig(tlc_blocks * block_size, tlc_streams),
        memory_policy=memory_policy or MemoryPolicyConfig(),
        storage_policy=storage_policy or StoragePolicyConfig(),
        simulation_end_ns=simulation_end_ns,
    )


def read_trace(path):
    with path.open(newline="", encoding="utf-8") as trace_file:
        return list(csv.DictReader(trace_file))


def test_baseline_dump_and_storage_hit_have_consistent_metrics(tmp_path):
    trace_path = tmp_path / "trace.csv"
    simulator = DWPDSimulator(
        config(
            storage_policy=StoragePolicyConfig(
                kind="baseline_fixed_lru",
                fixed_tier="slc",
                fixed_stream_id=1,
            )
        ),
        trace_path,
    )

    simulator.run(
        [
            Request(0, 1, 10, [1]),
            Request(1, 2, 20, [2]),
            Request(2, 3, 10, [1]),
        ]
    )
    simulator.finish()

    stats = simulator.stats()
    assert stats["accesses"] == {
        "requests": 3,
        "total": 3,
        "memory_hits": 0,
        "slc_hits": 1,
        "tlc_hits": 0,
        "global_misses": 2,
        "memory_hit_rate": 0.0,
        "storage_hit_rate": 1 / 3,
        "total_hit_rate": 1 / 3,
        "global_miss_rate": 2 / 3,
    }
    assert stats["dumps"]["admitted"] == {"segments": 2, "blocks": 2, "bytes": 1024}
    assert stats["storage"]["slc"]["host_write_bytes"] == 1024
    assert stats["storage"]["slc"]["program_bytes"] == 1024
    assert stats["storage"]["slc"]["stream_writes"]["1"]["blocks"] == 2
    assert stats["trace"] == {"schema_version": 4, "events": 3}

    rows = read_trace(trace_path)
    assert list(rows[0]) == [
        "sequence",
        "timestamp_ns",
        "request_id",
        "access_sequence",
        "operation",
        "storage_tier",
        "stream_id",
        "offset_bytes",
        "length_bytes",
        "node_id",
        "hash_id",
        "reason",
        "move_id",
        "depends_on_sequence",
    ]
    assert [row["operation"] for row in rows] == ["WRITE", "READ", "WRITE"]
    assert [row["reason"] for row in rows] == ["MEMORY_DUMP", "STORAGE_HIT", "MEMORY_DUMP"]


def test_dump_admission_is_atomic_and_rejection_drops_memory_segment(tmp_path):
    simulator = DWPDSimulator(
        config(
            memory_blocks=2,
            slc_blocks=1,
            storage_policy=StoragePolicyConfig(
                kind="baseline_fixed_lru",
                fixed_tier="slc",
            ),
        ),
        tmp_path / "rejected.csv",
    )
    simulator.process(0, 1, 1, [1, 2])
    simulator.process(1, 2, 2, [3])
    simulator.finish()

    stats = simulator.stats()
    assert stats["dumps"]["admitted"]["blocks"] == 0
    assert stats["dumps"]["rejected"] == {"segments": 1, "blocks": 2, "bytes": 1024}
    assert stats["errors"] == {
        "no_space": 1,
        "protected_victim_exhaustion": 0,
        "admission_rejections": 1,
    }
    assert stats["memory"]["drop_blocks"] == 2
    assert read_trace(tmp_path / "rejected.csv") == []


def test_memory_reclaim_is_leaf_first_and_greedy_by_segment(tmp_path):
    trace_path = tmp_path / "memory-segment-reclaim.csv"
    simulator = DWPDSimulator(
        config(
            memory_blocks=3,
            slc_blocks=16,
            storage_policy=StoragePolicyConfig(
                kind="baseline_fixed_lru",
                fixed_tier="slc",
                fixed_stream_id=1,
            ),
        ),
        trace_path,
    )
    simulator.run(
        [
            Request(0, 1, 1, [1, 2, 4]),
            Request(1, 2, 1, [1, 3]),
            Request(2, 3, 1, [1, 2, 4]),
            Request(3, 4, 1, [5]),
        ]
    )
    simulator.finish()

    stats = simulator.stats()
    assert stats["memory"]["evicted_segments"] == 4
    assert stats["memory"]["evicted_blocks"] == 6
    assert stats["memory"]["evictions_with_storage_copy"] == 2
    assert stats["memory"]["dump_segments"] == 3
    assert stats["memory"]["dump_blocks"] == 4
    assert stats["dumps"]["requests"] == 3
    rows = read_trace(trace_path)
    assert [row["operation"] for row in rows] == [
        "WRITE",
        "WRITE",
        "READ",
        "READ",
        "WRITE",
        "WRITE",
    ]
    assert [row["node_id"] for row in rows if row["operation"] == "WRITE"] == [
        "2",
        "4",
        "3",
        "1",
    ]


def test_baseline_slc_capacity_reclaim_relocates_before_reusing_space(tmp_path):
    trace_path = tmp_path / "capacity-relocation.csv"
    simulator = DWPDSimulator(
        config(
            slc_blocks=1,
            storage_policy=StoragePolicyConfig(
                kind="baseline_fixed_lru",
                fixed_tier="slc",
            ),
        ),
        trace_path,
    )
    simulator.run(
        [
            Request(0, 1, 1, [1]),
            Request(1, 2, 2, [2]),
            Request(2, 3, 3, [3]),
        ]
    )
    simulator.finish()

    stats = simulator.stats()
    assert stats["migrations"]["capacity"] == {"segments": 1, "blocks": 1, "bytes": 512}
    assert stats["foreground_capacity_evictions"] == {
        "segments": 1,
        "blocks": 1,
        "bytes": 512,
    }
    rows = read_trace(trace_path)
    assert [row["operation"] for row in rows] == ["WRITE", "READ", "WRITE", "TRIM", "WRITE"]
    moved = [row for row in rows if row["move_id"]]
    assert [row["reason"] for row in moved] == ["CAPACITY_EVICTION"] * 3
    assert moved[1]["depends_on_sequence"] == moved[0]["sequence"]
    assert moved[2]["depends_on_sequence"] == moved[1]["sequence"]


@pytest.mark.parametrize(
    ("kind", "expected_tier"),
    [
        ("wear_share_round_robin", "TLC"),
        ("wear_share_affinity", "SLC"),
        ("adaptive_endurance", "SLC"),
    ],
)
def test_policy_initial_placement_is_deterministic(tmp_path, kind, expected_tier):
    trace_path = tmp_path / f"{kind}.csv"
    simulator = DWPDSimulator(
        config(storage_policy=StoragePolicyConfig(kind=kind, logical_fill_fraction=1.0)),
        trace_path,
    )
    simulator.process(0, 1, 11, [1])
    simulator.process(1, 2, 22, [2])
    simulator.finish()
    rows = read_trace(trace_path)
    assert rows[0]["operation"] == "WRITE"
    assert rows[0]["storage_tier"] == expected_tier


def adaptive_endurance(
    *,
    background_period_ns=0,
    idle_multiplier=1e9,
    promotion_seconds=2.0,
):
    return StoragePolicyConfig(
        kind="adaptive_endurance",
        logical_fill_fraction=1.0,
        promotion_seconds=promotion_seconds,
        idle_multiplier=idle_multiplier,
        background_period_ns=background_period_ns,
    )


def test_access_migration_reuses_storage_hit_read(tmp_path):
    trace_path = tmp_path / "access-migration.csv"
    simulator = DWPDSimulator(config(storage_policy=adaptive_endurance()), trace_path)
    simulator.process(0, 1, 11, [1])
    simulator.process(0, 2, 22, [2])
    simulator.process(1_000_000_000, 3, 11, [1])
    simulator.finish()

    stats = simulator.stats()
    assert stats["migrations"]["access"] == {"segments": 1, "blocks": 1, "bytes": 512}
    assert stats["relocation"]["reused_access_reads"]["blocks"] == 1
    assert stats["relocation"]["explicit_reads"]["blocks"] == 0
    rows = [row for row in read_trace(trace_path) if row["move_id"]]
    assert [row["operation"] for row in rows] == ["READ", "WRITE", "TRIM"]
    assert [row["reason"] for row in rows] == [
        "STORAGE_HIT",
        "ACCESS_MIGRATION",
        "ACCESS_MIGRATION",
    ]
    assert [row["depends_on_sequence"] for row in rows] == ["", rows[0]["sequence"], rows[1]["sequence"]]
    assert len({row["move_id"] for row in rows}) == 1


def test_background_ticks_run_through_request_gap_and_finish(tmp_path):
    second = 1_000_000_000
    trace_path = tmp_path / "background.csv"
    simulator = DWPDSimulator(
        config(
            storage_policy=adaptive_endurance(background_period_ns=second),
            simulation_end_ns=3 * second,
        ),
        trace_path,
    )
    simulator.process(0, 1, 11, [1])
    simulator.process(0, 2, 22, [2])
    simulator.finish()

    stats = simulator.stats()
    assert stats["background"]["ticks"] == 3
    assert stats["migrations"]["background"]["segments"] == 1
    assert stats["relocation"]["explicit_reads"]["blocks"] == 1
    rows = [row for row in read_trace(trace_path) if row["reason"] == "BACKGROUND_MIGRATION"]
    assert [row["timestamp_ns"] for row in rows] == [str(second)] * 3
    assert [row["operation"] for row in rows] == ["READ", "WRITE", "TRIM"]


def test_background_relocation_preserves_last_logical_access_time(tmp_path):
    second = 1_000_000_000
    simulator = DWPDSimulator(
        config(
            storage_policy=adaptive_endurance(
                background_period_ns=second,
                idle_multiplier=0.0,
                promotion_seconds=4.0,
            ),
            simulation_end_ns=60 * second,
        ),
        tmp_path / "background-recency.csv",
    )
    simulator.process(0, 1, 11, [1])
    simulator.process(0, 2, 22, [2])
    simulator.finish()

    stats = simulator.stats()
    assert stats["migrations"]["background"]["segments"] == 1
    assert stats["background"]["idle_evictions"]["segments"] == 1
    assert stats["storage"]["tlc"]["live_bytes"] == 0


def test_batch_and_sequential_requests_are_identical_with_affinity_and_ticks(tmp_path):
    second = 1_000_000_000
    simulation_config = config(
        memory_blocks=2,
        storage_policy=adaptive_endurance(background_period_ns=second),
        simulation_end_ns=3 * second,
    )
    requests = [
        Request(0, 10, 77, [1, 2]),
        Request(second, 11, 77, [1, 3]),
        Request(2 * second, 12, 88, [4, 5]),
    ]

    sequential = DWPDSimulator(simulation_config, tmp_path / "sequential.csv")
    sequential.run(requests)
    sequential.finish()

    batch = DWPDSimulator(simulation_config, tmp_path / "batch.csv")
    batch.process_batch(
        np.asarray([0, second, 2 * second], dtype=np.uint64),
        np.asarray([10, 11, 12], dtype=np.uint64),
        np.asarray([77, 77, 88], dtype=np.uint64),
        np.asarray([0, 2, 4, 6], dtype=np.uint64),
        np.asarray([1, 2, 1, 3, 4, 5], dtype=np.uint64),
    )
    batch.finish()

    assert batch.stats() == sequential.stats()
    assert (tmp_path / "batch.csv").read_text(encoding="utf-8") == (
        tmp_path / "sequential.csv"
    ).read_text(encoding="utf-8")


def test_adaptive_endurance_observes_one_gap_per_request(tmp_path):
    simulator = DWPDSimulator(
        config(memory_blocks=8, storage_policy=adaptive_endurance()),
        tmp_path / "gaps.csv",
    )
    simulator.run(
        [
            Request(0, 1, 99, [1, 2]),
            Request(10, 2, 99, [1, 2]),
            Request(20, 3, 99, [1, 2]),
        ]
    )
    simulator.finish()
    assert simulator.stats()["algorithm"]["gap_samples"] == 2


def test_request_id_and_timestamp_contracts_are_enforced(tmp_path):
    simulator = DWPDSimulator(config(memory_blocks=2), tmp_path / "input.csv")
    simulator.process(10, 1, 0, [1])
    with pytest.raises(ValueError, match="request_id must be unique"):
        simulator.process(10, 1, 0, [2])
    with pytest.raises(ValueError, match="timestamp_ns moved backwards"):
        simulator.process(9, 2, 0, [2])
    simulator.finish()


def test_config_rejects_more_than_eight_total_streams(tmp_path):
    with pytest.raises(ValueError, match="stream counts must total at most 8"):
        DWPDSimulator(
            config(slc_streams=5, tlc_streams=4),
            tmp_path / "too-many-streams.csv",
        )
