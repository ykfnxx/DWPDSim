"""Public replay checks for session growth retention and gap-scaled tail eviction."""

from dataclasses import replace

import pytest
from test_memory_performance import configuration

from dwpdsim import DWPDSimulator, MemoryPolicyConfig, StoragePolicyConfig


def adaptive_config(memory=4, **options):
    return replace(configuration(memory, 32),
                   memory_policy=MemoryPolicyConfig(kind="context_lru", alpha=.01, **options),
                   storage_policy=StoragePolicyConfig(kind="infinite_storage"))


@pytest.mark.parametrize("beta,time,dropped", [(0, 75, 4), (5, 75, 0), (5, 80, 0), (5, 81, 4)])
def test_growth_retention_strict_boundary(tmp_path, beta, time, dropped):
    cfg = adaptive_config(retention_ns=60 * 10**9, retention_growth_seconds_per_block=beta)
    with DWPDSimulator(cfg, tmp_path / "unused.csv") as sim:
        sim.process(0, 0, 1, [1, 2])
        sim.process(10 * 10**9, 1, 1, [1, 2, 3, 4])
        sim.process(time * 10**9, 2, 2, [5])
        assert sim.stats()["memory"]["drop_blocks"] == dropped
        assert sim.stats()["memory"]["dump_blocks"] == 4 - dropped


@pytest.mark.parametrize("path,session", [([1, 2], 1), ([1, 2, 3, 4], 2)])
def test_shrinking_or_new_session_resets_growth(tmp_path, path, session):
    cfg = adaptive_config(retention_ns=60 * 10**9, retention_growth_seconds_per_block=100)
    with DWPDSimulator(cfg, tmp_path / "unused.csv") as sim:
        sim.process(0, 0, 1, [1, 2])
        sim.process(10 * 10**9, 1, 1, [1, 2, 3, 4])
        sim.process(20 * 10**9, 2, session, path)
        sim.process(81 * 10**9, 3, 3, [5])
        assert sim.stats()["memory"]["drop_blocks"] == 4


@pytest.mark.parametrize("session", [0, 1])
def test_first_request_has_no_growth_credit(tmp_path, session):
    cfg = adaptive_config(retention_ns=60 * 10**9, retention_growth_seconds_per_block=100)
    with DWPDSimulator(cfg, tmp_path / "unused.csv") as sim:
        sim.process(0, 0, session, [1, 2, 3, 4])
        sim.process(61 * 10**9, 1, 2, [5])
        assert sim.stats()["memory"]["drop_blocks"] == 4


@pytest.mark.parametrize("gap,removed", [(None, 4), (0, 1), (30, 2), (60, 4), (120, 6)])
@pytest.mark.parametrize("retention", [None, 60 * 10**9])
def test_gap_scales_both_actions_and_preserves_prefix(tmp_path, gap, removed, retention):
    cfg = adaptive_config(memory=8, retention_ns=retention, max_eviction_blocks=6,
                          eviction_base_blocks=4, eviction_gap_reference_ns=60 * 10**9)
    with DWPDSimulator(cfg, tmp_path / "unused.csv") as sim:
        sim.process(0, 0, 1, list(range(1, 9)))
        if gap is not None:
            # Repeated hash in this request must not erase the inter-request gap.
            sim.process(gap * 10**9, 1, 1, list(range(1, 9)) + [8])
        sim.process(((gap or 0) + 61) * 10**9, 2, 2, [9])
        stats = sim.stats()
        assert stats["memory"]["evicted_blocks"] == removed
        assert stats["memory"]["drop_blocks" if retention else "dump_blocks"] == removed
        before = stats["accesses"]["memory_hits"]
        sim.process(((gap or 0) + 62) * 10**9, 3, 1, list(range(1, 9 - removed)))
        assert sim.stats()["accesses"]["memory_hits"] - before == 8 - removed


def test_combined_growth_and_gap(tmp_path):
    cfg = adaptive_config(retention_ns=60 * 10**9, retention_growth_seconds_per_block=5,
                          max_eviction_blocks=4, eviction_base_blocks=4,
                          eviction_gap_reference_ns=20 * 10**9)
    with DWPDSimulator(cfg, tmp_path / "unused.csv") as sim:
        sim.process(0, 0, 1, [1, 2])
        sim.process(10 * 10**9, 1, 1, [1, 2, 3, 4])
        sim.process(75 * 10**9, 2, 2, [5])
        # Latest members are newly inserted: use base grain, and completed growth prevents Drop.
        assert sim.stats()["memory"]["dump_blocks"] == 4
        assert sim.stats()["memory"]["drop_blocks"] == 0


def test_diagnostics_preserve_replay_and_record_repeated_drop(tmp_path):
    import csv

    cfg = adaptive_config(memory=1, retention_ns=0)
    results = []
    for enabled in (False, True):
        with DWPDSimulator(cfg, tmp_path / "unused.csv") as sim:
            if enabled:
                sim.enable_memory_diagnostics(tmp_path / "evictions.csv")
            for index, block in enumerate((1, 2, 1, 2)):
                sim.process(index * 10**9, index, 1, [block])
        results.append(sim.stats())
    assert results[0] == results[1]
    with (tmp_path / "evictions.csv").open() as source:
        events = list(csv.DictReader(source))
    assert [int(e["node_id"]) for e in events] == [1, 2, 1]
    assert [e["action"] for e in events] == ["D"] * 3
    assert [int(e["sequence"]) for e in events] == [1, 2, 3]
