"""RR replay equivalence, including per-decision comparison with the scan oracle."""

import random
from dataclasses import replace

import pytest

from dwpdsim import (
    DWPDSimulator,
    MemoryConfig,
    MemoryPolicyConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)


def requests(seed, count=900):
    rng = random.Random(seed)
    for i in range(count):
        root = i + 16 if i % 9 == 0 else rng.randrange(16)
        branch = rng.randrange(4)
        prefix = [root * 1000 + j for j in range(4)]
        suffix = [root * 1000 + 100 + branch * 20 + j for j in range(12)]
        path = (prefix + suffix)[: rng.randrange(1, 17)]
        if i % 13 == 0:
            path.append(path[-1])
        yield i // 5, i, i % 7, path


def replay(path, config, rows):
    sim = DWPDSimulator(config, path)
    for row in rows:
        sim.process(*row)
    sim.finish()
    return sim.stats(), path.read_bytes(), sim.storage_performance()


@pytest.mark.parametrize("seed", [3, 19, 71])
@pytest.mark.parametrize(
    "memory,slc,tlc,admit",
    [
        (5, 32, 64, True),
        (17, 64, 96, False),
        (11, 32, 100_000, True),
    ],
)
def test_rr_exact_replay_and_every_victim(tmp_path, seed, memory, slc, tlc, admit):
    config = SimulationConfig(
        memory=MemoryConfig(memory * 512),
        slc=StorageTierConfig(slc * 512, 3),
        tlc=StorageTierConfig(tlc * 512, 2),
        block_size_bytes=512,
        memory_policy=MemoryPolicyConfig(kind="indexed_lru", admit_storage_hits=admit),
        storage_policy=StoragePolicyConfig(kind="wear_share_round_robin", rr_victim_search="scan"),
    )
    rows = list(requests(seed))
    expected = replay(tmp_path / "scan.csv", config, rows)
    assert expected[2]["decisions"] > 0
    for mode, counts in [("fused", False), ("indexed", False), ("indexed", True)]:
        actual = replay(
            tmp_path / f"{mode}-{counts}.csv",
            replace(
                config,
                storage_policy=replace(
                    config.storage_policy,
                    rr_victim_search=mode,
                    rr_subtree_counts=counts,
                    rr_verify_victims=True,
                    rr_profile=True,
                ),
            ),
            rows,
        )
        assert actual[:2] == expected[:2]
        assert actual[2]["verified_decisions"] == expected[2]["decisions"]
        assert actual[2]["decision_ns"] > 0


def test_rr_memory_hits_do_not_touch_storage_time(tmp_path):
    config = SimulationConfig(
        memory=MemoryConfig(2 * 512),
        slc=StorageTierConfig(4 * 512, 2),
        tlc=StorageTierConfig(4 * 512, 2),
        block_size_bytes=512,
        memory_policy=MemoryPolicyConfig(kind="indexed_lru"),
        storage_policy=StoragePolicyConfig(kind="wear_share_round_robin", rr_victim_search="scan"),
    )
    paths = [[0, 1], [2, 3], [0, 1], [1], [0, 1], [4, 5], [0, 6], [7, 8], [0, 1], [9, 10]]
    rows = [(i, i, 0, path) for i, path in enumerate(paths)]
    expected = replay(tmp_path / "scan.csv", config, rows)
    actual = replay(
        tmp_path / "index.csv",
        replace(
            config,
            storage_policy=replace(
                config.storage_policy,
                rr_victim_search="indexed",
                rr_verify_victims=True,
            ),
        ),
        rows,
    )
    assert actual[:2] == expected[:2]
    assert actual[2]["verified_decisions"] > 0
