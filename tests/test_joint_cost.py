"""Cost actions, raw logical demand, joint recovery and finite storage ledgers."""

import csv
from dataclasses import replace

import pytest

from dwpdsim import (
    DWPDSimulator,
    HybridAttentionConfig,
    HybridEvent,
    HybridPrefillRequest,
    JointCostConfig,
    MemoryConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)


def configuration(**cost):
    return SimulationConfig(
        memory=MemoryConfig(64), slc=StorageTierConfig(256, 2),
        tlc=StorageTierConfig(512, 2), block_size_bytes=8,
        storage_policy=StoragePolicyConfig(kind="wear_share_round_robin", slc_host_share=1/3),
        hybrid_attention=HybridAttentionConfig(
            enabled=True, full_block_bytes=8, linear_checkpoint_bytes=24,
            full_policy="lru_k", linear_policy="lru_k", replay_mode="prefix_replay",
            tokens_per_block=2, checkpoint_interval_tokens=2,
            checkpoint_generation="request_end", eviction_mode="joint_cost",
            cost=JointCostConfig(**{
                "horizons_seconds": (1, 3, 8), "sample_every": 1, "samples": 4,
                "nand_page_bytes": 8, "nand_pages_per_block": 4,
                "prefill_tokens_per_second": 1, "scenario_limit": 4, **cost,
            }),
        ),
    )


def requests(count=30):
    for i in range(count):
        root = i % 3
        length = 3 + i // 6
        yield HybridPrefillRequest(
            (i // 2) * 10**9, i, i, [root * 100 + j for j in range(1, length + 1)],
            input_tokens=2 * length + 1, affinity_id=root + 1,
        )


def legal_restore(sim, request):
    restored = 0
    for i, identity in enumerate(request.hash_ids):
        full = sim.hybrid_inspect(identity, "full")
        if not full["published"] or not (full["in_dram"] or full["on_storage"]):
            break
        linear = sim.hybrid_inspect(identity, "linear")
        if linear["published"] and (linear["in_dram"] or linear["on_storage"]):
            restored = 2 * (i + 1)
    return restored


def run(cfg, path, count=30):
    with DWPDSimulator(cfg, path) as sim:
        for request in requests(count):
            expected = legal_restore(sim, request)
            result = sim.process_hybrid_prefill(request)
            assert result["restore_tokens"] == expected
            assert sim.hybrid_stats()["pool"]["resident_bytes"] <= cfg.memory.capacity_bytes
        sim.finish()
        return sim.hybrid_stats()


def test_joint_cost_and_dump_all_share_input_and_recovery_contract(tmp_path):
    cfg = configuration()
    cost = run(cfg, tmp_path / "cost")
    baseline = run(replace(cfg, hybrid_attention=replace(
        cfg.hybrid_attention, eviction_mode="legacy", cost=JointCostConfig(),
    )), tmp_path / "baseline")
    assert cost["requests"] == baseline["requests"] == 30
    assert cost["prefill"]["input_tokens"] == baseline["prefill"]["input_tokens"]
    q = cost["cost"]
    assert q["decisions"] > 0 and q["quotes"] > q["decisions"]
    assert q["logical_full_demands"] == sum(len(r.hash_ids) for r in requests())
    assert q["pending_forecasts"] == 0
    assert q["total_cost"] == pytest.approx(
        q["drop_cost"] - q["saved_cost"] + q["replacement_cost"] +
        cfg.hybrid_attention.cost.wear_weight * q["media_cost"] + q["queue_cost"],
    )
    assert sum(s["samples"] for s in q["reuse"]["full"]) > 0
    assert sum(s["censored"] for s in q["reuse"]["full"]) > 0
    assert "cost" not in baseline


def test_logical_observation_survives_last_copy_loss(tmp_path):
    cfg = configuration(wear_weight=1e16)
    with DWPDSimulator(cfg, tmp_path / "drop") as sim:
        for request in requests():
            sim.process_hybrid_prefill(request)
        state = sim.hybrid_inspect(1, "full")
        assert state["logical_demands"] == 10
        assert state["demands"] <= state["logical_demands"]
        stats = sim.hybrid_stats()
        assert stats["full"]["drop_objects"] + stats["linear"]["drop_objects"] > 0
        assert stats["cost"]["logical_linear_demands"] > stats["linear"]["demands"]


@pytest.mark.parametrize("prices,preferred", [((0, 1e12), 0), ((1e12, 0), 1)])
def test_media_prices_change_selected_route(tmp_path, prices, preferred):
    result = run(configuration(nand_seconds_per_gib=prices), tmp_path / str(preferred))
    writes = result["cost"]["selected_route_writes"]
    assert writes[preferred] > 0
    assert writes[1-preferred] == 0


def test_cost_trace_keeps_storage_bytes_and_dependencies_consistent(tmp_path):
    path = tmp_path / "trace"
    stats = run(configuration(nand_seconds_per_gib=(0, 0)), path, count=54)
    live = {}
    rows = list(csv.DictReader(path.with_name(path.name + ".hybrid.csv").open()))
    for sequence, row in enumerate(rows):
        assert int(row["sequence"]) == sequence
        addresses = [int(address) for address in row["storage_addresses"].split(";")]
        identity = (row["component"], int(row["hash_id"]))
        for address in addresses:
            key = (row["storage_tier"], address)
            if row["operation"] == "WRITE":
                assert key not in live
                live[key] = identity
            else:
                assert live[key] == identity
                if row["operation"] == "TRIM":
                    del live[key]
        if row["depends_on_sequence"]:
            assert int(row["depends_on_sequence"]) < sequence
    for tier in ("slc", "tlc"):
        state = stats["storage"][tier]
        assert state["write_bytes"] - state["trim_bytes"] == state["used_bytes"]
        assert state["used_bytes"] == 8 * sum(key[0] == tier for key in live)
        assert state["peak_used_bytes"] <= state["capacity_bytes"]


def test_cost_replay_is_deterministic_with_trace_disabled(tmp_path):
    cfg = configuration(nand_seconds_per_gib=(0, 0))
    traced = run(cfg, tmp_path / "yes")
    quiet = run(replace(cfg, hybrid_attention=replace(cfg.hybrid_attention, emit_io_trace=False)),
                tmp_path / "no")
    assert traced == quiet


def test_pending_request_checkpoint_preserves_its_recoverable_prefix(tmp_path):
    cfg = configuration(nand_seconds_per_gib=(0, 0))
    with DWPDSimulator(cfg, tmp_path / "pending") as sim:
        for i, path in enumerate(([1, 2, 3, 4, 5], [11, 12, 13, 14, 15])):
            sim.process_hybrid_prefill(HybridPrefillRequest(i * 10**9, i, i, path, input_tokens=11))
        result = sim.process_hybrid_prefill(HybridPrefillRequest(
            2 * 10**9, 2, 2, [11, 12, 13, 14, 15], input_tokens=11,
        ))
        assert result["restore_tokens"] == 10


def test_completed_dump_does_not_delay_late_demand(tmp_path):
    cfg = configuration(horizons_seconds=(10, 30, 80), nand_seconds_per_gib=(0, 1e12),
                        storage_write_bytes_per_second=(100, 100),
                        prefill_tokens_per_second=0.01)
    with DWPDSimulator(cfg, tmp_path / "idle-queue") as sim:
        sim.process_hybrid_prefill(HybridPrefillRequest(0, 0, 0, [1, 2], input_tokens=5))
        sim.process_hybrid_prefill(HybridPrefillRequest(10 * 10**9, 1, 1, [101], input_tokens=3))
        stats = sim.hybrid_stats()
        assert stats["cost"]["decisions"] == 1
        assert sum(stats["cost"]["selected_route_writes"]) > 0
        assert stats["cost"]["queue_cost"] == 0


def test_pending_dump_is_readable_after_waiting_for_its_write(tmp_path):
    cfg = configuration(horizons_seconds=(0.0001, 0.0003, 0.0008),
                        nand_seconds_per_gib=(0, 1e12),
                        storage_write_bytes_per_second=(100, 100),
                        prefill_tokens_per_second=0.01)
    with DWPDSimulator(cfg, tmp_path / "pending-write") as sim:
        sim.process_hybrid_prefill(HybridPrefillRequest(0, 0, 0, [1, 2], input_tokens=5))
        sim.process_hybrid_prefill(HybridPrefillRequest(10 * 10**9, 1, 1, [101], input_tokens=3))
        stats = sim.hybrid_stats()
        assert sum(stats["cost"]["selected_route_writes"]) > 0
        assert stats["cost"]["queue_cost"] > 0
        assert sim.process_hybrid_prefill(HybridPrefillRequest(
            10 * 10**9, 2, 2, [1, 2], input_tokens=5,
        ))["restore_tokens"] == 4


def test_existing_write_backlog_adds_queue_wait(tmp_path):
    cfg = configuration(nand_seconds_per_gib=(0, 1e12),
                        storage_write_bytes_per_second=(100, 100),
                        prefill_tokens_per_second=0.01)
    with DWPDSimulator(cfg, tmp_path / "busy-queue") as sim:
        for i, (time, path) in enumerate(((0, [1, 2]), (10, [101]), (10, [201]))):
            sim.process_hybrid_prefill(HybridPrefillRequest(time * 10**9, i, i, path,
                                                          input_tokens=2 * len(path) + 1))
        assert sim.hybrid_stats()["cost"]["queue_cost"] > 0


def test_background_migration_preserves_model_storage_recency(tmp_path):
    cfg = configuration(nand_seconds_per_gib=(0, 1e12), prefill_tokens_per_second=0.01)
    cfg = replace(cfg, storage_policy=StoragePolicyConfig(
        kind="adaptive_endurance", logical_fill_fraction=1, slc_host_share=1/3,
        promotion_seconds=1, idle_multiplier=0.001, background_period_ns=10**9,
    ))
    with DWPDSimulator(cfg, tmp_path / "migration") as sim:
        sim.process_hybrid_prefill(HybridPrefillRequest(0, 0, 0, [1, 2], input_tokens=5))
        sim.process_hybrid_prefill(HybridPrefillRequest(0, 1, 1, [101], input_tokens=3))
        keys = [(identity, kind) for identity in (1, 2, 101) for kind in ("full", "linear")]
        stored = [(identity, kind) for identity, kind in keys
                  if sim.hybrid_inspect(identity, kind)["on_storage"]]
        assert stored
        sim.process_hybrid(HybridEvent(5 * 10**9, 2, "observe", [999]))
        for identity, kind in stored:
            state = sim.hybrid_inspect(identity, kind)
            assert state["storage_tier"] == "tlc"
            assert state["cost_model_copy_written_ns"] > 0
            assert state["cost_model_storage_last_ns"] == 0
            assert state["cost_model_copy_ready_seconds"] >= state["cost_model_copy_written_ns"] / 1e9


def test_disjoint_branches_keep_their_own_first_demand_mass(tmp_path):
    stats = run(configuration(candidates_per_kind=4, scenario_limit=8,
                              nand_seconds_per_gib=(0, 0)), tmp_path / "branches", count=54)
    cost = stats["cost"]
    assert cost["expected_first_requests"] > cost["decisions"]


def test_short_prompt_without_complete_hash_blocks_matches_baseline(tmp_path):
    cfg = configuration()
    observations = []
    for name, selected in (("cost", cfg), ("baseline", replace(
        cfg, hybrid_attention=replace(cfg.hybrid_attention, eviction_mode="legacy", cost=JointCostConfig()),
    ))):
        with DWPDSimulator(selected, tmp_path / name) as sim:
            result = sim.process_hybrid_prefill(HybridPrefillRequest(0, 0, 0, [], input_tokens=1))
            assert result["restore_tokens"] == 0 and result["prefill_compute_tokens"] == 1
            sim.process_hybrid_prefill(HybridPrefillRequest(10**9, 1, 1, [1, 2], input_tokens=5))
            sim.process_hybrid_prefill(HybridPrefillRequest(2 * 10**9, 2, 2, [], input_tokens=1))
            observations.append(sim.hybrid_stats())
    assert observations[0]["prefill"] == observations[1]["prefill"]
    assert observations[0]["cost"]["logical_full_demands"] == 2
