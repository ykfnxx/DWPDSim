import csv

import numpy as np

from dwpdsim import (
    DWPDSimulator,
    MediumConfig,
    MemoryPolicyConfig,
    PlacementPolicyConfig,
    Request,
    SimulationConfig,
)


def config(*, memory_blocks=1, slc_blocks=1, tlc_blocks=2, streams=2):
    block_size = 8
    return SimulationConfig(
        block_size_bytes=block_size,
        memory_capacity_bytes=memory_blocks * block_size,
        slc=MediumConfig(slc_blocks * block_size, streams),
        tlc=MediumConfig(tlc_blocks * block_size, streams),
        timestamp_unit="ticks",
    )


def read_trace(path):
    with path.open(newline="", encoding="utf-8") as trace_file:
        return list(csv.DictReader(trace_file))


def test_full_cache_flow_generates_consistent_metrics_and_trace(tmp_path):
    trace_path = tmp_path / "trace.csv"
    simulator = DWPDSimulator(
        config(),
        trace_path,
        placement_policy=PlacementPolicyConfig(
            kind="fixed",
            fixed_medium="slc",
            fixed_stream_id=1,
        ),
    )

    simulator.run(
        [
            Request(0, [1]),
            Request(1, [2]),
            Request(2, [1]),
            Request(3, [1]),
        ]
    )
    simulator.finish()

    stats = simulator.stats()
    assert stats["accesses"] == {
        "requests": 4,
        "total": 4,
        "memory_hits": 1,
        "slc_hits": 1,
        "tlc_hits": 0,
        "global_misses": 2,
        "memory_hit_rate": 0.25,
        "storage_hit_rate": 1 / 3,
        "total_hit_rate": 0.5,
        "global_miss_rate": 0.5,
    }
    assert stats["memory"]["evictions"] == 2
    assert stats["memory"]["evicted_segments"] == 2
    assert stats["memory"]["evicted_blocks"] == 2
    assert stats["memory"]["eviction_persists"] == 2
    assert stats["storage"]["slc"]["reads"]["blocks"] == 1
    assert stats["storage"]["slc"]["writes"]["blocks"] == 2
    assert stats["storage"]["slc"]["trims"]["blocks"] == 1
    assert stats["storage"]["slc"]["evicted_segments"] == 1
    assert stats["storage"]["slc"]["evicted_blocks"] == 1
    assert stats["storage"]["slc"]["demoted_segments"] == 1
    assert stats["storage"]["slc"]["demoted_blocks"] == 1
    assert stats["storage"]["slc"]["stream_writes"]["1"]["blocks"] == 2
    assert stats["storage"]["tlc"]["writes"]["blocks"] == 1
    assert stats["storage"]["tlc"]["stream_writes"]["0"]["blocks"] == 1
    assert stats["trace"]["schema_version"] == 2

    rows = read_trace(trace_path)
    assert [row["operation"] for row in rows] == [
        "WRITE",
        "READ",
        "WRITE",
        "TRIM",
        "WRITE",
    ]
    assert [row["medium"] for row in rows] == ["SLC", "SLC", "TLC", "SLC", "SLC"]
    assert [row["stream_id"] for row in rows] == ["1", "1", "0", "1", "1"]
    assert [row["reason"] for row in rows] == [
        "MEMORY_EVICTION",
        "STORAGE_HIT",
        "SLC_DEMOTION",
        "SLC_DEMOTION",
        "MEMORY_EVICTION",
    ]
    assert [(row["node_id"], row["hash_id"]) for row in rows] == [
        ("1", "1"),
        ("1", "1"),
        ("1", "1"),
        ("1", "1"),
        ("2", "2"),
    ]


def test_batch_and_request_interfaces_share_prefixes_identically(tmp_path):
    requests = [Request(10, [1, 2]), Request(20, [1, 3]), Request(30, [4, 5])]

    request_simulator = DWPDSimulator(
        config(memory_blocks=8, slc_blocks=8, tlc_blocks=8),
        tmp_path / "request.csv",
    )
    request_simulator.run(requests)
    request_simulator.finish()

    batch_simulator = DWPDSimulator(
        config(memory_blocks=8, slc_blocks=8, tlc_blocks=8),
        tmp_path / "batch.csv",
    )
    batch_simulator.process_batch(
        np.asarray([10, 20, 30], dtype=np.uint64),
        np.asarray([0, 2, 4, 6], dtype=np.uint64),
        np.asarray([1, 2, 1, 3, 4, 5], dtype=np.uint64),
    )
    batch_simulator.finish()

    assert batch_simulator.stats() == request_simulator.stats()
    assert batch_simulator.node_count == 5
    assert batch_simulator.stats()["accesses"]["memory_hits"] == 1
    assert batch_simulator.stats()["accesses"]["global_misses"] == 5


def test_storage_hit_can_bypass_memory(tmp_path):
    simulator = DWPDSimulator(
        config(slc_blocks=2),
        tmp_path / "bypass.csv",
        memory_policy=MemoryPolicyConfig(
            admit_storage_hits=False,
            eviction_action="persist",
        ),
        placement_policy=PlacementPolicyConfig(fixed_medium="slc"),
    )

    simulator.process(0, [10])
    simulator.process(1, [20])
    simulator.process(2, [10])
    simulator.finish()

    stats = simulator.stats()
    assert stats["memory"]["storage_bypasses"] == 1
    assert stats["memory"]["storage_promotions"] == 0
    assert stats["memory"]["resident_blocks"] == 1
    assert stats["storage"]["slc"]["reads"]["blocks"] == 1


def test_evicting_a_memory_copy_does_not_rewrite_storage(tmp_path):
    simulator = DWPDSimulator(
        config(slc_blocks=2),
        tmp_path / "existing-copy.csv",
        placement_policy=PlacementPolicyConfig(fixed_medium="slc"),
    )

    for timestamp, hash_id in enumerate([1, 2, 1, 3]):
        simulator.process(timestamp, [hash_id])
    simulator.finish()

    stats = simulator.stats()
    assert stats["memory"]["evictions"] == 3
    assert stats["memory"]["evictions_with_storage_copy"] == 1
    assert stats["storage"]["slc"]["writes"]["blocks"] == 2
    assert [row["operation"] for row in read_trace(tmp_path / "existing-copy.csv")] == [
        "WRITE",
        "READ",
        "WRITE",
    ]


def test_drop_policy_recomputes_an_absent_tree_node(tmp_path):
    simulator = DWPDSimulator(
        config(),
        tmp_path / "drop.csv",
        memory_policy=MemoryPolicyConfig(eviction_action="drop"),
    )

    for timestamp, hash_id in enumerate([1, 2, 1]):
        simulator.process(timestamp, [hash_id])
    simulator.finish()

    stats = simulator.stats()
    assert stats["accesses"]["global_misses"] == 3
    assert stats["memory"]["eviction_drops"] == 2
    assert stats["trace"]["events"] == 0
    assert simulator.node_count == 1
    assert stats["tree"] == {"nodes": 1, "nodes_created": 3, "nodes_removed": 2}


def test_memory_and_storage_eviction_use_segment_batches(tmp_path):
    simulator = DWPDSimulator(
        config(memory_blocks=3, slc_blocks=3, tlc_blocks=3),
        tmp_path / "segments.csv",
        placement_policy=PlacementPolicyConfig(fixed_medium="slc"),
    )

    simulator.process(0, [1, 2, 3])
    simulator.process(1, [4])
    assert simulator.stats()["memory"]["evicted_segments"] == 1
    assert simulator.stats()["memory"]["evicted_blocks"] == 3

    simulator.process(2, [5])
    simulator.process(3, [6])
    simulator.process(4, [7])
    simulator.finish()

    stats = simulator.stats()
    assert stats["storage"]["slc"]["evicted_segments"] == 1
    assert stats["storage"]["slc"]["evicted_blocks"] == 3
    assert stats["storage"]["slc"]["demoted_segments"] == 1
    assert stats["storage"]["slc"]["demoted_blocks"] == 3
    rows = read_trace(tmp_path / "segments.csv")
    assert [row["operation"] for row in rows] == ["WRITE"] * 3 + [
        "WRITE",
        "TRIM",
    ] * 3 + ["WRITE"]
    assert [row["reason"] for row in rows[3:9]] == ["SLC_DEMOTION"] * 6


def test_slc_demotion_evicts_full_tlc_before_destination_write(tmp_path):
    trace_path = tmp_path / "nested-demotion.csv"
    simulator = DWPDSimulator(
        config(memory_blocks=1, slc_blocks=1, tlc_blocks=1),
        trace_path,
        placement_policy=PlacementPolicyConfig(
            kind="fixed",
            fixed_medium="slc",
            fixed_stream_id=1,
        ),
    )

    for timestamp, hash_id in enumerate([1, 2, 3, 4]):
        simulator.process(timestamp, [hash_id])
    simulator.finish()

    stats = simulator.stats()
    assert stats["storage"]["slc"]["demoted_segments"] == 2
    assert stats["storage"]["slc"]["demoted_blocks"] == 2
    assert stats["storage"]["slc"]["reads"]["blocks"] == 0
    assert stats["storage"]["tlc"]["evicted_segments"] == 1
    assert stats["storage"]["tlc"]["evicted_blocks"] == 1
    assert stats["storage"]["tlc"]["demoted_blocks"] == 0
    assert stats["storage"]["tlc"]["reads"]["blocks"] == 0
    rows = read_trace(trace_path)
    assert [(row["operation"], row["medium"], row["reason"]) for row in rows[4:7]] == [
        ("TRIM", "TLC", "STORAGE_EVICTION"),
        ("WRITE", "TLC", "SLC_DEMOTION"),
        ("TRIM", "SLC", "SLC_DEMOTION"),
    ]


def test_ratio_placement_distributes_media_and_streams(tmp_path):
    simulator = DWPDSimulator(
        config(slc_blocks=4, tlc_blocks=4),
        tmp_path / "ratio.csv",
        placement_policy=PlacementPolicyConfig(kind="ratio", slc_write_ratio=0.5),
    )

    for timestamp, hash_id in enumerate([1, 2, 3, 4, 5]):
        simulator.process(timestamp, [hash_id])
    simulator.finish()

    writes = [row for row in read_trace(tmp_path / "ratio.csv") if row["operation"] == "WRITE"]
    assert [(row["medium"], row["stream_id"]) for row in writes] == [
        ("SLC", "0"),
        ("TLC", "0"),
        ("SLC", "1"),
        ("TLC", "1"),
    ]
