"""Public replay tests for the fixed-WA wear-balanced storage policy."""

from dataclasses import replace

import pytest
from test_simulator import config, read_trace

from dwpdsim import DWPDSimulator, MemoryPolicyConfig, StoragePolicyConfig


def policy(**kwargs):
    return replace(StoragePolicyConfig(
        kind="wear_balanced", logical_fill_fraction=1.0,
        slc_erase_budget=1, tlc_erase_budget=1,
        background_period_ns=0, promotion_seconds=1e9,
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
    assert ("estimated_slc_pressure" in sim.stats()["algorithm"]) == (kind == "wear_balanced")


@pytest.mark.parametrize("slc_wa,tlc_wa,target,age,migrations", [
    (1, 1, 0.5, 8, 0),
    (4, 1, 0.8, 4, 1),
    (1, 4, 0.2, 32, 0),
])
def test_fixed_wa_changes_budget_pressure_and_access_migration(
    tmp_path, slc_wa, tlc_wa, target, age, migrations,
):
    path = tmp_path / "wa.csv"
    with DWPDSimulator(config(slc_blocks=8, tlc_blocks=8, storage_policy=policy(
        slc_wa=slc_wa, tlc_wa=tlc_wa, promotion_seconds=8,
    )), path) as sim:
        for i in range(3):
            sim.process(0, i, 10, [i + 1])
        stats = sim.stats()["algorithm"]
        assert stats["slc_program_bytes"] == stats["tlc_program_bytes"] == 512
        assert stats["target_tlc_share"] == pytest.approx(target)
        assert stats["estimated_slc_pressure"] == pytest.approx(slc_wa / 8)
        assert stats["estimated_tlc_pressure"] == pytest.approx(tlc_wa / 8)
        assert stats["effective_promotion_seconds"] == pytest.approx(age)
        sim.process(4_000_000_000, 3, 10, [1])
    assert sim.stats()["migrations"]["access"]["segments"] == migrations
    rows = [row for row in read_trace(path) if row["move_id"]]
    assert [row["operation"] for row in rows] == ["READ", "WRITE", "TRIM"] * migrations


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


@pytest.mark.parametrize("wa", [0.9, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["slc_wa", "tlc_wa"])
def test_invalid_fixed_wa_is_rejected_at_public_boundary(tmp_path, field, wa):
    with pytest.raises(ValueError, match="WA must be finite and at least 1"):
        DWPDSimulator(config(storage_policy=policy(**{field: wa})), tmp_path / "bad.csv")


@pytest.mark.parametrize("tlc_wa,tier", [(1, "TLC"), (4, "SLC")])
def test_fixed_wa_changes_initial_placement(tmp_path, tlc_wa, tier):
    path = tmp_path / "placement.csv"
    with DWPDSimulator(config(slc_blocks=8, tlc_blocks=8, storage_policy=policy(
        tlc_wa=tlc_wa,
    )), path) as sim:
        for i in range(3):
            sim.process(i, i, 5, [i + 1])
    # Same affinity and writes: TLC WA reduces t from 0.5 to 0.2,
    # moving the second dump from TLC to SLC.
    assert [row["storage_tier"] for row in read_trace(path)] == ["SLC", tier]


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
