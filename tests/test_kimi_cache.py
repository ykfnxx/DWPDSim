"""Observable FULL/LINEAR recovery and LRU-K behavior through the public API."""

from array import array
from dataclasses import replace

import pytest

from dwpdsim import (
    DWPDSimulator,
    HybridAttentionConfig,
    HybridEvent,
    HybridPrefillRequest,
    MemoryConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
    kimi_k3_profile,
)


def config(**overrides):
    hybrid = HybridAttentionConfig(**{
        "pool_allocation": "fixed",
        "enabled": True,
        "full_policy": "lru_k",
        "linear_policy": "lru_k",
        "lru_k": 2,
        "full_block_bytes": 11,
        "linear_checkpoint_bytes": 23,
        "full_capacity_bytes": 22,
        "linear_capacity_bytes": 46,
        "emit_io_trace": False,
        **overrides,
    })
    return SimulationConfig(
        memory=MemoryConfig(hybrid.full_capacity_bytes + hybrid.linear_capacity_bytes),
        slc=StorageTierConfig(8, 1),
        tlc=StorageTierConfig(8, 1),
        block_size_bytes=8,
        storage_policy=StoragePolicyConfig(kind="infinite_storage"),
        hybrid_attention=hybrid,
    )


def event(seq, action, path, component="both"):
    return HybridEvent(seq * 10, seq, action, path, request_id=seq, components=component)


def prefill(seq, path):
    return HybridPrefillRequest(seq * 10, seq, seq, path, input_tokens=len(path) * 64 + 1)


def prefix_config(**overrides):
    return config(**{
        "replay_mode": "prefix_replay",
        "checkpoint_generation": "request_end",
        "tokens_per_block": 64,
        "checkpoint_interval_tokens": 256,
        **overrides,
    })


@pytest.mark.parametrize("block_tokens,full_bytes", [(16, 442_368), (64, 1_769_472)])
def test_kimi_profile_replays_with_distinct_whole_model_byte_budgets(
    tmp_path, block_tokens, full_bytes,
):
    profile = kimi_k3_profile(tokens_per_block=block_tokens)
    cfg = SimulationConfig(
        memory=MemoryConfig(2 * full_bytes + 2 * 449_372_160),
        slc=StorageTierConfig(1 << 20, 1),
        tlc=StorageTierConfig(1 << 20, 1),
        block_size_bytes=1 << 20,
        storage_policy=StoragePolicyConfig(kind="infinite_storage"),
        hybrid_attention=profile.hybrid_config(
            pool_allocation="fixed",
            full_capacity_bytes=2 * full_bytes, linear_capacity_bytes=2 * 449_372_160,
        ),
    )
    with DWPDSimulator(cfg, tmp_path / "kimi-bytes") as sim:
        for seq, path in enumerate([[1, 2], [1, 2, 3], [10, 11]]):
            sim.process_hybrid_prefill(HybridPrefillRequest(
                seq * 10, seq, seq, path, input_tokens=len(path) * block_tokens + 1,
            ))
        assert sim.hybrid_inspect(11, "full")["size_bytes"] == full_bytes
        assert sim.hybrid_inspect(11, "linear")["size_bytes"] == 449_372_160
        stats = sim.hybrid_stats()
        assert stats["full"]["resident_bytes"] == 2 * full_bytes
        assert stats["linear"]["resident_bytes"] == 2 * 449_372_160
        assert stats["full"]["evictions"] > 0 and stats["linear"]["evictions"] > 0


def test_component_sizes_and_residency_are_independent(tmp_path):
    with DWPDSimulator(config(linear_capacity_bytes=23), tmp_path / "components") as sim:
        sim.process_hybrid(event(0, "publish", [1], "full"))
        sim.process_hybrid(event(1, "publish", [1], "linear"))
        sim.process_hybrid(event(2, "publish", [2], "full"))
        sim.process_hybrid(event(3, "publish", [2], "linear"))
        assert sim.hybrid_inspect(1, "full")["in_dram"]
        assert not sim.hybrid_inspect(1, "linear")["in_dram"]
        assert sim.hybrid_inspect(1, "linear")["on_storage"]
        sim.process_hybrid(event(4, "publish", [3], "full"))
        assert not sim.hybrid_inspect(1, "full")["in_dram"]
        assert sim.hybrid_inspect(2, "linear")["in_dram"]
        stats = sim.hybrid_stats()
        assert stats["full"]["resident_bytes"] == 22
        assert stats["linear"]["resident_bytes"] == 23
        assert stats["full"]["write_bytes"] == 11
        assert stats["linear"]["write_bytes"] == 23


@pytest.mark.parametrize("component", ["full", "linear"])
def test_a_single_readable_component_cannot_restore_a_hybrid_prefix(tmp_path, component):
    cfg = prefix_config(full_capacity_bytes=44, linear_capacity_bytes=92)
    with DWPDSimulator(cfg, tmp_path / component) as sim:
        if component == "full":
            for seq, path in enumerate([[1], [1, 2], [1, 2, 3]]):
                sim.process_hybrid(event(seq, "publish", path, "full"))
        else:
            sim.process_hybrid(event(0, "publish", [1, 2, 3], "linear"))
        result = sim.process_hybrid_prefill(prefill(4, [1, 2, 3]))
        assert result["restore_tokens"] == 0
        assert result["restore_hash_id"] is None
        assert result["linear"] == "initial"
        assert sim.hybrid_stats()["prefill"]["restored_tokens"] == 0
        assert sim.hybrid_stats()["linear"]["demands"] == 0


def test_joint_restore_stops_at_a_middle_full_gap(tmp_path):
    cfg = prefix_config(linear_capacity_bytes=92, dump_on_eviction=False)
    with DWPDSimulator(cfg, tmp_path / "middle-gap") as sim:
        for seq, path in enumerate([[1, 2], [1], [1, 2, 3]]):
            sim.process_hybrid(event(seq, "publish", path, "full"))
        sim.process_hybrid(event(3, "publish", [1], "linear"))
        sim.process_hybrid(event(4, "publish", [1, 2, 3], "linear"))
        assert sim.hybrid_inspect(1, "full")["in_dram"]
        assert sim.hybrid_inspect(3, "full")["in_dram"]
        assert not sim.hybrid_inspect(2, "full")["in_dram"]
        assert not sim.hybrid_inspect(2, "full")["on_storage"]
        assert sim.hybrid_inspect(3, "linear")["in_dram"]
        result = sim.process_hybrid_prefill(prefill(5, [1, 2, 3, 4]))
        assert result["full_available_prefix_tokens"] == result["restore_tokens"] == 64
        assert result["restore_hash_id"] == 1
        assert result["full"] == ["dram"]
        assert sim.hybrid_inspect(1, "linear")["demands"] == 1
        assert sim.hybrid_inspect(3, "linear")["demands"] == 0


@pytest.mark.parametrize("ssd_component", ["full", "linear"])
def test_joint_restore_accepts_complete_sources_across_dram_and_ssd(tmp_path, ssd_component):
    cfg = prefix_config(
        full_capacity_bytes=11 if ssd_component == "full" else 44,
        linear_capacity_bytes=23 if ssd_component == "linear" else 92,
    )
    with DWPDSimulator(cfg, tmp_path / ssd_component) as sim:
        sim.process_hybrid(event(0, "publish", [1], "full"))
        sim.process_hybrid(event(1, "publish", [1, 2], "full"))
        sim.process_hybrid(event(2, "publish", [1, 2], "linear"))
        if ssd_component == "linear":
            sim.process_hybrid(event(3, "publish", [9], "linear"))
        result = sim.process_hybrid_prefill(prefill(4, [1, 2, 3]))
        assert result["restore_tokens"] == 128
        assert result["restore_hash_id"] == 2
        assert result["full"] == (["storage", "dram"] if ssd_component == "full"
                                   else ["dram", "dram"])
        assert result["linear"] == ("storage" if ssd_component == "linear" else "dram")
        assert sim.hybrid_stats()["prefill"]["restored_requests"] == 1


def test_only_the_consumed_checkpoint_receives_a_reference(tmp_path):
    cfg = prefix_config(full_capacity_bytes=1100, linear_capacity_bytes=230)
    with DWPDSimulator(cfg, tmp_path / "endpoint-demand") as sim:
        for seq, path in enumerate([[1, 2], [1, 2, 3, 4], [100, 101], [1, 2, 3, 4, 5, 6]]):
            sim.process_hybrid_prefill(prefill(seq, path))
        assert sim.hybrid_inspect(2, "linear")["demands"] == 1
        assert sim.hybrid_inspect(4, "linear")["demands"] == 1
        assert sim.hybrid_inspect(101, "linear")["demands"] == 0
        assert sim.hybrid_inspect(6, "linear")["demands"] == 0
        assert sim.hybrid_stats()["linear"]["demands"] == 2


@pytest.mark.parametrize("component", ["full", "linear"])
@pytest.mark.parametrize("k,evicted", [(1, 1), (2, 2)])
def test_lru_k_retains_hot_objects_and_k_one_uses_last_demand(tmp_path, component, k, evicted):
    with DWPDSimulator(config(lru_k=k), tmp_path / f"{component}-{k}") as sim:
        for seq, action, identity in [(0, "publish", 1), (1, "request", 1),
                                      (2, "request", 1), (3, "publish", 2),
                                      (4, "request", 2), (5, "publish", 3)]:
            sim.process_hybrid(event(seq, action, [identity], component))
        assert not sim.hybrid_inspect(evicted, component)["in_dram"]
        assert sim.hybrid_inspect(evicted, component)["on_storage"]
        assert sim.hybrid_inspect(3 - evicted, component)["in_dram"]
        assert sim.hybrid_inspect(3, component)["in_dram"]


@pytest.mark.parametrize("component", ["full", "linear"])
def test_eviction_and_ssd_reload_preserve_lru_k_reference_history(tmp_path, component):
    with DWPDSimulator(config(), tmp_path / component) as sim:
        actions = [("publish", 1), ("request", 1), ("request", 1),
                   ("publish", 2), ("request", 2), ("publish", 3)]
        for seq, (action, identity) in enumerate(actions):
            sim.process_hybrid(event(seq, action, [identity], component))
        assert not sim.hybrid_inspect(2, component)["in_dram"]
        result = sim.process_hybrid(event(6, "request", [2], component))
        assert (result["full"] == ["storage"] if component == "full"
                else result["linear"] == "storage")
        assert sim.hybrid_inspect(2, component)["in_dram"]
        sim.process_hybrid(event(7, "publish", [4], component))
        # Reload is the second demand for 2. Its Kth reference is newer than 1's.
        assert sim.hybrid_inspect(2, component)["in_dram"]
        assert not sim.hybrid_inspect(1, component)["in_dram"]
        assert sim.hybrid_stats()[component]["storage_hits"] == 1


def workload():
    return [prefill(seq, path) for seq, path in enumerate([
        [1, 2], [1, 2, 3], [10, 11], [1, 2, 3], [20, 21],
        [1, 2, 3, 4], [10, 11], [30, 31], [1, 2, 3, 4, 5],
    ])]


def test_trace_switch_preserves_results_and_stats_without_creating_a_trace(tmp_path):
    outputs = []
    for enabled in [False, True]:
        path = tmp_path / str(enabled)
        with DWPDSimulator(prefix_config(emit_io_trace=enabled), path) as sim:
            results = [sim.process_hybrid_prefill(q) for q in workload()]
            stats = sim.hybrid_stats()
        assert path.with_name(path.name + ".hybrid.csv").exists() == enabled
        assert not path.exists()
        assert stats["full"]["evictions"] > 0 and stats["linear"]["evictions"] > 0
        outputs.append((results, stats))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("component,size", [("full", 11), ("linear", 23)])
def test_no_trace_omits_addresses_but_preserves_ssd_availability_and_io_bytes(
    tmp_path, component, size,
):
    allocated = ((size + 7) // 8) * 8
    for enabled in [False, True]:
        with DWPDSimulator(config(emit_io_trace=enabled), tmp_path / str(enabled)) as sim:
            for seq, identity in enumerate([1, 2, 3]):
                sim.process_hybrid(event(seq, "publish", [identity], component))
            stored = sim.hybrid_inspect(1, component)
            assert stored["on_storage"] and not stored["in_dram"]
            assert bool(stored["storage_addresses"]) == enabled
            assert sim.hybrid_stats()[component]["write_allocated_bytes"] == allocated
            result = sim.process_hybrid(event(3, "request", [1], component))
            assert (result["full"] == ["storage"] if component == "full"
                    else result["linear"] == "storage")
            assert sim.hybrid_stats()[component]["read_bytes"] == size
            assert sim.hybrid_stats()[component]["read_allocated_bytes"] == allocated


def test_lru_k_prefill_batch_matches_scalar_results_and_stats(tmp_path):
    rows = workload()
    hashes, offsets = [], [0]
    for q in rows:
        hashes.extend(q.hash_ids)
        offsets.append(len(hashes))
    columns = [array("Q", values) for values in [
        [q.timestamp_ns for q in rows], [q.sequence for q in rows],
        [q.request_id for q in rows], [q.input_tokens for q in rows],
        [2**64 - 1] * len(rows), offsets, hashes,
    ]]
    outputs = []
    for batch in [False, True]:
        cfg = replace(prefix_config(), progress_interval_requests=0)
        with DWPDSimulator(cfg, tmp_path / str(batch)) as sim:
            results = (sim.process_hybrid_prefill_batch(*columns) if batch
                       else [sim.process_hybrid_prefill(q) for q in rows])
            outputs.append((results, sim.hybrid_stats()))
    assert outputs[0] == outputs[1]


def test_aggregate_replay_matches_scalar_joint_stats(tmp_path):
    rows = workload()
    with DWPDSimulator(prefix_config(), tmp_path / "aggregate") as sim:
        aggregate = sim.run_hybrid_prefill(rows, batch_requests=3)
    with DWPDSimulator(prefix_config(), tmp_path / "scalar") as sim:
        for q in rows:
            sim.process_hybrid_prefill(q)
        scalar = sim.hybrid_stats()
    assert aggregate == scalar
    assert 0 < scalar["prefill"]["restored_tokens"] < scalar["prefill"]["input_tokens"]
