"""Request-end publication, endpoint-only LRU use, and independent recovery ledger."""

import random
from array import array
from collections import OrderedDict
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
)


def config(**overrides):
    h = HybridAttentionConfig(**{
        "pool_allocation": "fixed",
        "enabled": True, "replay_mode": "prefix_replay", "checkpoint_generation": "request_end",
        "tokens_per_block": 64, "checkpoint_interval_tokens": 256,
        "full_block_bytes": 11, "linear_checkpoint_bytes": 23,
        "full_capacity_bytes": 100000, "linear_capacity_bytes": 69,
        "full_policy": "indexed_segment_lru", "eviction_mode": "retention", **overrides,
    })
    return SimulationConfig(memory=MemoryConfig(h.full_capacity_bytes + h.linear_capacity_bytes),
        slc=StorageTierConfig(8, 1), tlc=StorageTierConfig(8, 1), block_size_bytes=8,
        storage_policy=StoragePolicyConfig(kind="infinite_storage"), hybrid_attention=h)


def request(seq, path, **kwargs):
    return HybridPrefillRequest(seq * 10, seq, seq, path, input_tokens=len(path) * 64 + 1, **kwargs)


def test_one_endpoint_per_request_and_no_inner_or_partial_tail_states(tmp_path):
    with DWPDSimulator(config(), tmp_path / "endpoints") as sim:
        first = sim.process_hybrid_prefill(request(0, [1, 2, 3]))
        assert first["restore_tokens"] == 0
        assert first["generated_checkpoint_tokens"] == [192]
        assert not sim.hybrid_inspect(1, "linear")["published"]
        second = sim.process_hybrid_prefill(request(1, [1, 2, 3, 4, 5]))
        assert second["restore_tokens"] == 192
        assert second["generated_checkpoint_tokens"] == [320]
        repeat = sim.process_hybrid_prefill(request(2, [1, 2, 3, 4, 5]))
        assert repeat["restore_tokens"] == 320 and repeat["prefill_compute_tokens"] == 1
        assert repeat["generated_checkpoint_tokens"] == [320]
        fork = sim.process_hybrid_prefill(request(3, [1, 2, 3, 4, 9]))
        assert fork["restore_tokens"] == 192
        assert fork["generated_checkpoint_tokens"] == [320]
        assert sim.hybrid_inspect(3, "linear")["demands"] == 2
        assert sim.hybrid_inspect(5, "linear")["demands"] == 1
        assert sim.hybrid_stats()["linear"]["demands"] == 3
        short = sim.process_hybrid_prefill(request(4, []))
        assert short["generated_checkpoint_tokens"] == [] and short["restore_tokens"] == 0


def test_lru_evicts_unused_ancestor_instead_of_unrelated_state(tmp_path):
    with DWPDSimulator(config(), tmp_path / "ancestor-lru") as sim:
        sim.process_hybrid_prefill(request(0, [1, 2]))
        sim.process_hybrid_prefill(request(1, [1, 2, 3, 4]))
        sim.process_hybrid_prefill(request(2, [100, 101]))
        result = sim.process_hybrid_prefill(request(3, [1, 2, 3, 4, 5, 6]))
        assert result["restore_tokens"] == 256
        assert sim.hybrid_inspect(2, "linear")["demands"] == 1
        assert sim.hybrid_inspect(2, "linear")["last_demand_ns"] == 10
        assert not sim.hybrid_inspect(2, "linear")["in_dram"]
        assert sim.hybrid_inspect(2, "linear")["on_storage"]
        assert sim.hybrid_inspect(101, "linear")["in_dram"]
        assert sim.hybrid_inspect(101, "linear")["demands"] == 0
        assert sim.hybrid_inspect(4, "linear")["demands"] == 1
        assert sim.hybrid_stats()["linear"]["demands"] == 2


def test_duplicate_publication_and_lookup_are_not_lru_use(tmp_path):
    with DWPDSimulator(config(linear_capacity_bytes=46), tmp_path / "republish") as sim:
        sim.process_hybrid_prefill(request(0, [1, 2]))
        sim.process_hybrid_prefill(request(1, [10, 11]))
        result = sim.process_hybrid_prefill(request(2, [1, 2], max_prefix_tokens=0))
        assert result["restore_tokens"] == 0
        assert result["generated_checkpoint_tokens"] == [128]
        sim.process_hybrid_prefill(request(3, [20, 21]))
        assert not sim.hybrid_inspect(2, "linear")["in_dram"]
        assert sim.hybrid_inspect(11, "linear")["in_dram"]
        assert sim.hybrid_stats()["linear"]["demands"] == 0


@pytest.mark.parametrize("policy", ["checkpoint_lru", "frontier_lru"])
def test_full_middle_gap_limits_joint_hit_to_earlier_linear_state(tmp_path, policy):
    cfg = config(full_capacity_bytes=22, linear_capacity_bytes=92,
                 full_retention_ns=0, linear_policy=policy)
    with DWPDSimulator(cfg, tmp_path / "middle-gap") as sim:
        for seq, path in enumerate([[1], [1, 2], [1, 2, 3], [1]]):
            sim.process_hybrid(HybridEvent(seq * 10, seq, "publish", path, components="full"))
        for seq, path in enumerate([[1], [1, 2, 3]], 4):
            sim.process_hybrid(HybridEvent(seq * 10, seq, "publish", path, components="linear"))
        assert sim.hybrid_inspect(1, "full")["in_dram"]
        assert sim.hybrid_inspect(3, "full")["in_dram"]
        missing = sim.hybrid_inspect(2, "full")
        assert missing["published"] and not missing["in_dram"] and not missing["on_storage"]
        assert sim.hybrid_inspect(3, "linear")["in_dram"]
        row = sim.process_hybrid_prefill(request(6, [1, 2, 3, 4]))
        assert row["full_available_prefix_tokens"] == row["restore_tokens"] == 64
        assert row["restore_hash_id"] == 1 and row["full"] == ["dram"]
        assert row["prefill_compute_tokens"] == 193
        assert sim.hybrid_inspect(1, "linear")["demands"] == 1
        assert sim.hybrid_inspect(3, "linear")["demands"] == 0


@pytest.mark.parametrize("selector", ["checkpoint_lru", "frontier_lru"])
@pytest.mark.parametrize("action", ["idle", "frontier"])
@pytest.mark.parametrize("retention", [None, 0, 10])
def test_request_end_matches_complete_path_ledger(tmp_path, selector, action, retention):
    cfg = config(linear_capacity_bytes=46, linear_policy=selector, alpha=1,
                 linear_retention_policy=action, linear_retention_ns=retention)
    resident, stored, endpoints, full = OrderedDict(), set(), set(), set()
    rng, now, next_hash = random.Random(38), 0, 1
    sessions = [[], [], []]
    uses = dropped = dumped = 0

    def admit(key):
        nonlocal dropped, dumped
        while len(resident) >= 2:
            covered = lambda c: any(len(d) > len(c) and d[:len(c)] == c for d in [*resident, key])
            victim = next(iter(resident))
            if selector == "frontier_lru":
                victim = next((c for c in resident if covered(c)), victim)
            drop = (retention is not None and now - resident[victim] > retention) or (
                action == "frontier" and covered(victim))
            if victim not in stored:
                if drop:
                    dropped += 1
                else:
                    stored.add(victim)
                    dumped += 1
            resident.pop(victim)
        resident[key] = now

    with DWPDSimulator(cfg, tmp_path / "ledger") as sim:
        for seq in range(120):
            now += rng.choice([0, 2, 20])
            sid = rng.randrange(3)
            path = sessions[sid][:rng.randrange(len(sessions[sid]) + 1)]
            count = rng.randrange(1, 8)
            path += list(range(next_hash, next_hash + count))
            next_hash += count
            sessions[sid] = path
            prefixes = [tuple(path[:i]) for i in range(1, len(path) + 1)]
            cap = rng.choice([len(path) * 64, len(path) * 32, 0])
            candidates = [p for p in prefixes if p in endpoints and len(p) * 64 <= cap
                          and (p in resident or p in stored)]
            chosen = max(candidates, key=len, default=())
            matched = next((i * 64 for i, p in enumerate(prefixes) if p not in full), len(path) * 64)
            source = "initial"
            if chosen:
                uses += 1
                source = "dram" if chosen in resident else "storage"
                if chosen in resident:
                    resident[chosen] = now
                    resident.move_to_end(chosen)
                else:
                    admit(chosen)
            full.update(prefixes)
            endpoint = tuple(path)
            endpoints.add(endpoint)
            if endpoint not in resident and endpoint not in stored:
                admit(endpoint)
            result = sim.process_hybrid_prefill(HybridPrefillRequest(
                now, seq, seq, path, input_tokens=len(path) * 64 + 1, max_prefix_tokens=cap))
            assert result["restore_tokens"] == len(chosen) * 64
            assert result["linear"] == source and result["matched_prefix_tokens"] == matched
            assert result["generated_checkpoint_tokens"] == [len(path) * 64]
            for endpoint in endpoints:
                state = sim.hybrid_inspect(endpoint[-1], "linear")
                assert state["in_dram"] == (endpoint in resident)
                assert state["on_storage"] == (endpoint in stored)
            stats = sim.hybrid_stats()
            assert stats["linear"]["demands"] == uses == stats["prefill"]["restored_requests"]
            assert stats["linear"]["drop_objects"] == dropped
            assert stats["linear"]["dump_objects"] == dumped


def test_request_end_batch_matches_sequential_and_exact_repeat_cap(tmp_path):
    rows = [request(0, [1, 2]), request(1, [1, 2, 3]), request(2, [7, 8]),
            HybridPrefillRequest(30, 3, 3, [1, 2, 3], input_tokens=192)]
    outputs = []
    for batch in [False, True]:
        with DWPDSimulator(config(), tmp_path / str(batch)) as sim:
            if batch:
                hashes, offsets = [], [0]
                for q in rows:
                    hashes.extend(q.hash_ids)
                    offsets.append(len(hashes))
                result = sim.process_hybrid_prefill_batch(*[array("Q", c) for c in [
                    [q.timestamp_ns for q in rows], [q.sequence for q in rows], [q.request_id for q in rows],
                    [q.input_tokens for q in rows], [2**64 - 1] * len(rows), offsets, hashes]])
            else:
                result = [sim.process_hybrid_prefill(q) for q in rows]
            assert result[-1]["restore_tokens"] == 128
            outputs.append((result, sim.hybrid_stats()))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("change", [
    {"checkpoint_generation": "bad"}, {"enabled": False}, {"replay_mode": "exact_objects"},
])
def test_request_end_mode_requires_valid_enabled_prefix_replay(tmp_path, change):
    cfg = config()
    cfg = replace(cfg, hybrid_attention=replace(cfg.hybrid_attention, **change))
    with pytest.raises(ValueError):
        DWPDSimulator(cfg, tmp_path / "invalid")
