"""Public retention decisions, shared byte accounting and recovery source oracles."""

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


def config(*, adaptive=False, total=140, **overrides):
    h = HybridAttentionConfig(**{
        "enabled": True, "replay_mode": "prefix_replay", "tokens_per_block": 64,
        "checkpoint_interval_tokens": 256, "full_block_bytes": 11, "linear_checkpoint_bytes": 23,
        "full_capacity_bytes": 100000 if not adaptive else 0,
        "linear_capacity_bytes": 46 if not adaptive else 0,
        "full_policy": "indexed_segment_lru", "eviction_mode": "retention",
        "pool_allocation": "adaptive" if adaptive else "fixed", **overrides,
    })
    return SimulationConfig(
        memory=MemoryConfig(total if adaptive else h.full_capacity_bytes + h.linear_capacity_bytes),
        slc=StorageTierConfig(8, 1), tlc=StorageTierConfig(8, 1), block_size_bytes=8,
        storage_policy=StoragePolicyConfig(kind="infinite_storage"), hybrid_attention=h,
    )


def test_idle_clock_starts_at_admission_and_threshold_is_strict(tmp_path):
    cfg = config(linear_capacity_bytes=23, linear_retention_ns=10)
    epoch = 10**15
    with DWPDSimulator(cfg, tmp_path / "clock") as sim:
        for seq, offset in enumerate([0, 10, 21, 21]):
            sim.process_hybrid(HybridEvent(epoch + offset, seq, "publish", [seq + 1], components="linear"))
        c = sim.hybrid_stats()["linear"]
        assert (c["evictions"], c["drop_objects"], c["dump_objects"]) == (3, 1, 2)
        assert sim.hybrid_inspect(1, "linear")["on_storage"]
        assert not sim.hybrid_inspect(2, "linear")["on_storage"]
        assert sim.hybrid_inspect(3, "linear")["on_storage"]
        assert c["evicted_bytes"] == c["eligible_bytes"] == 69
        assert c["lost_bytes"] == c["drop_action_bytes"] == 23
        assert c["dump_bytes"] == c["write_bytes"] == 46


@pytest.mark.parametrize("selector", ["checkpoint_lru", "frontier_lru"])
@pytest.mark.parametrize("action", ["idle", "frontier"])
@pytest.mark.parametrize("retention", [None, 0, 10])
def test_retention_matches_independent_complete_path_ledger(tmp_path, selector, action, retention):
    cfg = config(linear_policy=selector, linear_retention_policy=action, linear_retention_ns=retention, alpha=1)
    resident, stored, generated, full = OrderedDict(), set(), set(), set()
    rng, now, next_hash = random.Random(8), 10**15, 1
    sessions = [[], [], []]
    dropped = dumped = 0

    def admit(key):
        nonlocal dropped, dumped
        while len(resident) >= 2:
            covered = lambda c: any(len(d) > len(c) and d[:len(c)] == c for d in [*resident, key])
            victim = next(iter(resident))
            if selector == "frontier_lru":
                victim = next((c for c in resident if covered(c)), victim)
            drop = retention is not None and now - resident[victim] > retention
            drop = drop or (action == "frontier" and covered(victim))
            if victim not in stored:
                if drop:
                    dropped += 1
                else:
                    stored.add(victim)
                    dumped += 1
            resident.pop(victim)
        resident[key] = now

    with DWPDSimulator(cfg, tmp_path / "ledger") as sim:
        for seq in range(100):
            now += rng.choice([0, 2, 20])
            sid = rng.randrange(3)
            path = sessions[sid][:rng.randrange(len(sessions[sid]) + 1)]
            count = rng.randrange(1, 17)
            path += list(range(next_hash, next_hash + count))
            next_hash += count
            sessions[sid] = path
            prefixes = [tuple(path[:i]) for i in range(1, len(path) + 1)]
            matched = next((i * 64 for i, p in enumerate(prefixes) if p not in full), len(path) * 64)
            candidates = [p for p in prefixes if p in generated and (p in resident or p in stored)]
            chosen = max(candidates, key=len, default=())
            source = "initial"
            if chosen:
                source = "dram" if chosen in resident else "storage"
                if chosen in resident:
                    resident[chosen] = now
                    resident.move_to_end(chosen)
                else:
                    admit(chosen)
            for p in prefixes[len(chosen):]:
                full.add(p)
                if len(p) % 4 == 0:
                    generated.add(p)
                    if p not in resident and p not in stored:
                        admit(p)
            row = sim.process_hybrid_prefill(HybridPrefillRequest(now, seq, seq, path, input_tokens=len(path) * 64 + 1))
            assert row["restore_tokens"] == len(chosen) * 64
            assert row["linear"] == source
            assert row["matched_prefix_tokens"] == matched
            assert row["full"] == ["dram"] * len(chosen)
            for p in generated:
                state = sim.hybrid_inspect(p[-1], "linear")
                assert state["in_dram"] == (p in resident)
                assert state["on_storage"] == (p in stored)
        c = sim.hybrid_stats()["linear"]
        assert (c["drop_objects"], c["dump_objects"]) == (dropped, dumped)


@pytest.mark.parametrize("selector", ["checkpoint_lru", "frontier_lru", "target_value"])
@pytest.mark.parametrize("action", ["idle", "frontier"])
@pytest.mark.parametrize("generation", ["chunked_prefill", "request_end"])
def test_adaptive_byte_budget_and_pre_request_source_oracle(tmp_path, selector, action, generation):
    cfg = config(adaptive=True, linear_policy=selector, linear_retention_policy=action,
                 full_retention_ns=5, linear_retention_ns=5, alpha=1, checkpoint_generation=generation)
    rng, next_hash, now, known = random.Random(19), 1, 10**15, set()
    sessions = [[], [], []]
    with DWPDSimulator(cfg, tmp_path / "shared") as sim:
        for seq in range(140):
            sid = rng.randrange(3)
            path = sessions[sid][:rng.randrange(len(sessions[sid]) + 1)]
            count = rng.randrange(1, 15)
            path += list(range(next_hash, next_hash + count))
            next_hash += count
            sessions[sid] = path
            n = len(path) * 64 + rng.randrange(64)
            now += rng.choice([0, 2, 20])
            cap = rng.randrange(n) if seq % 5 == 0 else n - 1
            before = {k: [sim.hybrid_inspect(k, c) for c in ["full", "linear"]] for k in path}
            readable = lambda c: c["in_dram"] or c["on_storage"]
            full_ok, chosen, matched = True, 0, 0
            for i, k in enumerate(path, 1):
                f, l = before[k]
                if not f["published"]:
                    break
                matched = i * 64
                full_ok = full_ok and readable(f)
                if full_ok and readable(l) and i * 64 <= min(n - 1, cap):
                    chosen = i
            row = sim.process_hybrid_prefill(HybridPrefillRequest(now, seq, seq, path, input_tokens=n, max_prefix_tokens=cap))
            assert row["restore_tokens"] == chosen * 64
            assert row["matched_prefix_tokens"] == matched
            assert row["full"] == ["dram" if before[k][0]["in_dram"] else "storage" for k in path[:chosen]]
            expected_linear = ("dram" if before[path[chosen - 1]][1]["in_dram"] else "storage") if chosen else "initial"
            assert row["linear"] == expected_linear
            known.update(path)
            stats = sim.hybrid_stats()
            assert stats["pool"]["full_target_bytes"] + stats["pool"]["linear_target_bytes"] == 140
            resident = 0
            for component in ["full", "linear"]:
                actual = sum(sim.hybrid_inspect(k, component)["size_bytes"] for k in known if sim.hybrid_inspect(k, component)["in_dram"])
                assert stats[component]["resident_bytes"] == actual
                resident += actual
            assert resident <= stats["pool"]["peak_resident_bytes"] <= 140
        assert stats["pool"]["updates"] == 140
        assert stats["pool"]["cross_pool_reclaims"] > 0


def test_adaptive_targets_follow_causal_demand_and_pool_can_borrow(tmp_path):
    cfg = config(adaptive=True, total=1000, pool_decay_seconds=1)
    with DWPDSimulator(cfg, tmp_path / "targets") as sim:
        sim.process_hybrid_prefill(HybridPrefillRequest(0, 0, 0, list(range(1, 9)), input_tokens=513))
        p = sim.hybrid_stats()["pool"]
        assert p["full_target_bytes"] == int(1000 * 88 / (88 + 23))
        sim.process_hybrid_prefill(HybridPrefillRequest(10**12, 1, 1, [100], input_tokens=65))
        p = sim.hybrid_stats()["pool"]
        assert p["full_target_bytes"] == 977
        # No eager reclaim when the target moves below existing LINEAR occupancy.
        assert sim.hybrid_stats()["linear"]["resident_bytes"] > p["linear_target_bytes"]


def test_full_admission_cannot_claim_to_be_a_pending_linear_descendant(tmp_path):
    cfg = config(adaptive=True, total=69, linear_retention_policy="frontier")
    with DWPDSimulator(cfg, tmp_path / "cross-pool") as sim:
        initial = sim.hybrid_stats()["pool"]
        assert initial["full_target_bytes"] + initial["linear_target_bytes"] == 69
        events = [([1, 2, 3, 4], "linear"), ([10, 11, 12, 13], "linear"),
                  ([1, 2, 3, 4, 5], "full"), ([1, 2, 3, 4, 5, 6], "full"),
                  ([1, 2, 3, 4, 5, 6, 7], "full")]
        for seq, (path, component) in enumerate(events):
            sim.process_hybrid(HybridEvent(0, seq, "publish", path, components=component))
        assert sim.hybrid_inspect(4, "linear")["on_storage"]
        c = sim.hybrid_stats()["linear"]
        assert (c["dump_objects"], c["drop_objects"]) == (1, 0)


def test_frontier_retention_loss_on_short_branch_is_not_counted_as_a_hit(tmp_path):
    base = list(range(1, 13))
    branch = base[:4] + list(range(100, 112))
    results = {}
    for selector, action in [("checkpoint_lru", "idle"), ("frontier_lru", "frontier")]:
        cfg = config(linear_policy=selector, linear_retention_policy=action, alpha=1)
        with DWPDSimulator(cfg, tmp_path / selector) as sim:
            for seq, path in enumerate([base, branch, branch[:12]]):
                row = sim.process_hybrid_prefill(HybridPrefillRequest(seq, seq, seq, path, input_tokens=len(path) * 64 + 1))
            results[selector] = row["restore_tokens"]
            assert row["prefill_compute_tokens"] == 769 - row["restore_tokens"]
    assert results == {"checkpoint_lru": 768, "frontier_lru": 0}


@pytest.mark.parametrize("selector", ["segment_lru", "indexed_segment_lru"])
def test_adaptive_scalar_and_buffer_equivalence(tmp_path, selector):
    cfg = config(adaptive=True, full_policy=selector, linear_retention_policy="frontier", linear_retention_ns=10)
    paths = [list(range(1, 17)), list(range(1, 9)) + list(range(30, 42)), list(range(1, 17))]
    requests = [HybridPrefillRequest(i * 20, i, i, p, input_tokens=len(p) * 64 + 1) for i, p in enumerate(paths)]
    outputs = []
    for batch in [False, True]:
        with DWPDSimulator(cfg, tmp_path / str(batch)) as sim:
            if batch:
                rows = sim.process_hybrid_prefill_batch(
                    array('Q', [q.timestamp_ns for q in requests]), array('Q', range(3)), array('Q', range(3)),
                    array('Q', [q.input_tokens for q in requests]), array('Q', [2**64 - 1] * 3),
                    array('Q', [0, 16, 36, 52]), array('Q', [h for p in paths for h in p]),
                )
            else:
                rows = [sim.process_hybrid_prefill(q) for q in requests]
            outputs.append((rows, sim.hybrid_stats()))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("override", [
    {"full_drop_fraction": 0.5}, {"linear_drop_fraction": 0.5}, {"linear_policy": "frontier_guard"},
    {"dump_on_eviction": False}, {"pool_allocation": "other"},
])
def test_production_retention_rejects_synthetic_actions(tmp_path, override):
    with pytest.raises(ValueError):
        DWPDSimulator(config(adaptive=True, **override), tmp_path / "invalid")


def test_new_options_still_require_hybrid_flag(tmp_path):
    cfg = config(adaptive=True)
    with pytest.raises(ValueError):
        DWPDSimulator(replace(cfg, hybrid_attention=replace(cfg.hybrid_attention, enabled=False)), tmp_path / "disabled")
