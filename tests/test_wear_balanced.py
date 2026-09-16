"""Public replay tests for the Shadow-backed wear-balanced storage policy."""

from dataclasses import replace

import pytest
from test_simulator import config, read_trace

from dwpdsim import DWPDSimulator, MemoryPolicyConfig, StoragePolicyConfig


def policy(**kwargs):
    return replace(StoragePolicyConfig(
        kind="wear_balanced", logical_fill_fraction=1.0,
        slc_erase_budget=1, tlc_erase_budget=1,
        background_period_ns=0, promotion_seconds=1e9, shadow_page_bytes=512,
    ), **kwargs)


@pytest.mark.parametrize("kind,tiers", [
    ("adaptive_endurance", ["SLC", "TLC", "SLC"]),
    ("wear_balanced", ["SLC", "TLC", "TLC"]),
])
def test_target_share_keeps_tlc_placement_at_balance(tmp_path, kind, tiers):
    path = tmp_path / "balance.csv"
    with DWPDSimulator(config(
        slc_blocks=8, tlc_blocks=8,
        memory_policy=MemoryPolicyConfig(kind="context_lru", max_eviction_blocks=1),
        storage_policy=policy(kind=kind),
    ), path) as sim:
        # Affinity 1 selects TLC at p=0.5. The first dump always seeds SLC.
        for i in range(4):
            sim.process(i, i, 1, [i + 1])
    assert [row["storage_tier"] for row in read_trace(path)] == tiers
    assert ("shadow_slc_pressure" in sim.stats()["algorithm"]) == (kind == "wear_balanced")


def test_slc_capacity_boost_overrides_low_tlc_target(tmp_path):
    path = tmp_path / "capacity.csv"
    with DWPDSimulator(config(slc_blocks=20, tlc_blocks=20, storage_policy=policy(
        slc_erase_budget=9, direct_gain=0,
    )), path) as sim:
        # With t=0.1 and gain=0, affinity 4 keeps all initial writes on SLC.
        for i in range(20):
            sim.process(i, i, 4, [i + 1])
        # At 95% SLC occupancy, the capacity floor raises p from 0.1 to 0.55.
        sim.process(20, 20, 1, [21])
    rows = read_trace(path)
    assert [row["storage_tier"] for row in rows] == ["SLC"] * 19 + ["TLC"]


def test_capacity_reclaim_trims_before_dump_and_preserves_live_capacity(tmp_path):
    path = tmp_path / "reclaim.csv"
    with DWPDSimulator(config(slc_blocks=2, tlc_blocks=2, storage_policy=policy()), path) as sim:
        # Affinity 4 exceeds the 0.75 probability ceiling, so all dumps use SLC.
        for i in range(6):
            sim.process(i, i, 4, [i + 1])
    rows = read_trace(path)
    assert [row["operation"] for row in rows] == ["WRITE", "WRITE"] + ["TRIM", "WRITE"] * 3
    assert sim.stats()["storage"]["slc"]["live_bytes"] == 1024
    assert sim.stats()["errors"]["no_space"] == 0


def windows(path):
    return read_trace(path.with_name(path.name + ".controller_windows.csv"))


def test_shadow_counts_gc_and_feedback_changes_wa(tmp_path):
    path = tmp_path / "gc.csv"
    with DWPDSimulator(config(
        slc_blocks=12, tlc_blocks=12, slc_streams=1, tlc_streams=1,
        simulation_end_ns=100,
        storage_policy=policy(shadow_pages_per_block=4, shadow_overprovisioning=0.25,
                              feedback_period_ns=100),
    ), path) as sim:
        for i in range(15):
            sim.process(i, i, 4, [i + 1])
    stats = sim.stats()
    slc = stats["shadow_ftl"]["slc"]
    assert slc["host_program_pages"] == 14
    assert slc["gc_program_pages"] > 0
    assert slc["erases"] > 0
    assert stats["algorithm"]["slc_wa"] > 1
    assert stats["algorithm"]["target_tlc_share"] > 0.5
    assert stats["algorithm"]["shadow_slc_pressure"] == pytest.approx(
        (slc["host_program_pages"] + slc["gc_program_pages"]) / 12
    )


def test_feedback_boundaries_precede_same_time_timer_and_request(tmp_path):
    second = 1_000_000_000
    path = tmp_path / "ordering.csv"
    with DWPDSimulator(config(
        slc_blocks=8, tlc_blocks=8, simulation_end_ns=3 * second,
        storage_policy=policy(feedback_period_ns=second, background_period_ns=second,
                              promotion_seconds=2),
    ), path) as sim:
        sim.process(0, 1, 1, [1])
        sim.process(0, 2, 1, [2])
        sim.process(2 * second, 3, 1, [3])
    rows = windows(path)
    assert [int(row["timestamp_ns"]) for row in rows] == [second, 2 * second, 3 * second]
    assert int(rows[0]["slc_host_pages"]) == 1
    assert int(rows[0]["tlc_host_pages"]) == 0
    assert int(rows[1]["tlc_host_pages"]) == 1  # migration at t=1 belongs to [1,2)
    assert int(rows[2]["slc_host_pages"]) + int(rows[2]["tlc_host_pages"]) == 3
    migrations = [r for r in read_trace(path) if r["reason"] == "BACKGROUND_MIGRATION"]
    assert [r["operation"] for r in migrations] == ["READ", "WRITE", "TRIM"]
    assert all(int(r["timestamp_ns"]) == second for r in migrations)


def test_ghost_counts_lost_prefix_but_not_fresh_suffix(tmp_path):
    path = tmp_path / "ghost.csv"
    with DWPDSimulator(config(
        memory_policy=MemoryPolicyConfig(kind="context_lru", retention_ns=0),
        storage_policy=policy(online_tuning=True, feedback_period_ns=10),
        simulation_end_ns=10,
    ), path) as sim:
        sim.process(0, 1, 1, [1])
        sim.process(1, 2, 1, [2])  # Drop 1, no SSD copy
        sim.process(2, 3, 1, [1, 3])  # 1 reusable, 3 fresh
    assert sim.stats()["shadow_ftl"]["ghost_miss_blocks"] == 1
    assert windows(path)[0]["ghost_miss_blocks"] == "1"
    assert windows(path)[0]["reusable_blocks"] == "1"


def test_storage_trim_with_memory_copy_does_not_create_ghost_miss(tmp_path):
    second = 1_000_000_000
    path = tmp_path / "copies.csv"
    with DWPDSimulator(config(
        storage_policy=policy(online_tuning=True, background_period_ns=1000 * second),
    ), path) as sim:
        sim.process(0, 1, 4, [1])
        sim.process(1, 2, 4, [2])
        sim.process(2, 3, 4, [1])  # 1 now in both tiers
        sim.process(7000 * second, 4, 4, [1])  # idle Trim has preserved the Memory copy
    assert sim.stats()["background"]["idle_evictions"]["blocks"] == 2
    assert sim.stats()["shadow_ftl"]["ghost_miss_blocks"] == 0
    assert sim.stats()["shadow_ftl"]["ghost_entries"] == 1  # only block 2 lost its last copy


def test_online_idle_updates_on_confident_windows_and_freezes_on_empty_windows(tmp_path):
    second = 1_000_000_000
    path = tmp_path / "controller.csv"
    with DWPDSimulator(config(
        memory_policy=MemoryPolicyConfig(kind="context_lru", retention_ns=0),
        storage_policy=policy(online_tuning=True, feedback_period_ns=second,
                              min_reuse_blocks=1, reuse_ema_scale_blocks=1),
        simulation_end_ns=8 * second,
    ), path) as sim:
        request_id = 0
        for window in range(5):
            for offset in range(4):
                sim.process(window * second + (offset + 1) * second // 5,
                            request_id, 1, [1 + offset % 2])
                request_id += 1
        sim.process(5 * second, request_id, 1, [])
        learned = sim.stats()["algorithm"]["learned_idle_multiplier"]
        assert learned > 24
    assert sim.stats()["algorithm"]["learned_idle_multiplier"] == learned
    assert len(windows(path)) == 8


def test_ghost_ttl_expires_without_a_matching_request(tmp_path):
    hour = 3600 * 1_000_000_000
    with DWPDSimulator(config(
        memory_policy=MemoryPolicyConfig(kind="context_lru", retention_ns=0),
        storage_policy=policy(online_tuning=True, feedback_period_ns=hour),
        simulation_end_ns=25 * hour,
    ), tmp_path / "ttl.csv") as sim:
        sim.process(0, 1, 1, [1])
        sim.process(1, 2, 1, [2])
        assert sim.stats()["shadow_ftl"]["ghost_entries"] == 1
    assert sim.stats()["shadow_ftl"]["ghost_entries"] == 0


def test_repeated_loss_refreshes_ghost_lifetime(tmp_path):
    hour = 3600 * 1_000_000_000
    with DWPDSimulator(config(
        memory_policy=MemoryPolicyConfig(kind="context_lru", retention_ns=0),
        storage_policy=policy(online_tuning=True, feedback_period_ns=hour),
    ), tmp_path / "refresh.csv") as sim:
        sim.process(0, 1, 1, [1])
        sim.process(hour, 2, 1, [2])
        sim.process(2 * hour, 3, 1, [1])
        sim.process(3 * hour, 4, 1, [2])  # refresh ghost 1 at hour 3
        before = sim.stats()["shadow_ftl"]["ghost_miss_blocks"]
        sim.process(26 * hour, 5, 1, [1])
        assert sim.stats()["shadow_ftl"]["ghost_miss_blocks"] == before + 1


def test_online_batch_matches_sequential_including_windows(tmp_path):
    import numpy as np

    from dwpdsim import Request

    requests = [Request(i * 3, i, 1, [1 + i % 2]) for i in range(30)]
    cfg = config(
        memory_policy=MemoryPolicyConfig(kind="context_lru", retention_ns=0),
        storage_policy=policy(online_tuning=True, feedback_period_ns=10,
                              min_reuse_blocks=1, reuse_ema_scale_blocks=1),
        simulation_end_ns=100,
    )
    paths = [tmp_path / "sequential.csv", tmp_path / "batch.csv"]
    with DWPDSimulator(cfg, paths[0]) as sequential:
        sequential.run(requests)
    with DWPDSimulator(cfg, paths[1]) as batch:
        batch.process_batch(
            np.array([r.timestamp_ns for r in requests], dtype=np.uint64),
            np.arange(30, dtype=np.uint64), np.ones(30, dtype=np.uint64),
            np.arange(31, dtype=np.uint64),
            np.array([1 + i % 2 for i in range(30)], dtype=np.uint64),
        )
    assert sequential.stats() == batch.stats()
    assert paths[0].read_bytes() == paths[1].read_bytes()
    assert windows(paths[0]) == windows(paths[1])


@pytest.mark.parametrize("kwargs", [
    {"shadow_page_bytes": 1024}, {"shadow_pages_per_block": 0},
    {"feedback_period_ns": 0}, {"shadow_overprovisioning": 1},
    {"shadow_slc_nominal_bytes": 512}, {"min_reuse_blocks": 0},
    {"reuse_loss_budget": float("nan")},
])
def test_invalid_shadow_configuration_fails_at_public_boundary(tmp_path, kwargs):
    with pytest.raises(ValueError, match="shadow"):
        DWPDSimulator(config(storage_policy=policy(**kwargs)), tmp_path / "invalid.csv")


def test_nominal_capacity_is_independent_of_live_cache_capacity(tmp_path):
    with DWPDSimulator(config(slc_blocks=8, tlc_blocks=8, simulation_end_ns=10,
        storage_policy=policy(feedback_period_ns=10, shadow_slc_nominal_bytes=64 * 512,
                              shadow_slc_physical_blocks=32, shadow_pages_per_block=4),
    ), tmp_path / "nominal.csv") as sim:
        sim.process(0, 1, 4, [1])
        sim.process(1, 2, 4, [2])
    assert sim.stats()["shadow_ftl"]["slc"]["physical_blocks"] == 32
    assert sim.stats()["algorithm"]["shadow_slc_pressure"] == pytest.approx(1 / 64)


def test_finish_partial_window_reports_counts_without_applying_future_feedback(tmp_path):
    path = tmp_path / "partial.csv"
    with DWPDSimulator(config(simulation_end_ns=5, storage_policy=policy(feedback_period_ns=10)), path) as sim:
        sim.process(0, 1, 4, [1])
        sim.process(1, 2, 4, [2])
    assert sim.stats()["shadow_ftl"]["slc"]["host_program_pages"] == 1
    assert sim.stats()["algorithm"]["feedback_windows"] == 0
    assert windows(path) == []
