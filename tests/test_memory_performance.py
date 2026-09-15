"""Observable replay equivalence for the indexed MemoryPolicy and batch input."""

import csv
import io
import random
from dataclasses import replace

import pytest

from dwpdsim import (
    DWPDSimulator,
    InputConfig,
    MemoryConfig,
    MemoryPolicyConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
    parquet_batches,
    replay_batches,
)


def workload(seed=17, count=450):
    rng = random.Random(seed)
    paths = [[1, 2, 3, 4], [1, 2, 5], [1, 6], [7, 8, 9], [7, 10], [11]]
    for i in range(count):
        if i % 7 == 0:
            path = [1000 + i * 4 + j for j in range(4)]
        else:
            path = list(rng.choice(paths))
            if i % 11 == 0:
                path = path[: rng.randrange(len(path) + 1)]
            if i % 13 == 0 and path:
                path.append(path[-1])
        yield i // 3, i, i % 5, path


def configuration(memory, storage, admit=True):
    return SimulationConfig(
        memory=MemoryConfig(memory * 512),
        slc=StorageTierConfig(storage * 512, 2),
        tlc=StorageTierConfig(storage * 512, 2),
        block_size_bytes=512,
        memory_policy=MemoryPolicyConfig(admit_storage_hits=admit),
    )


def run(tmp_path, name, cfg, requests):
    path = tmp_path / f"{name}.csv"
    sim = DWPDSimulator(cfg, path)
    for request in requests:
        sim.process(*request)
    sim.finish()
    return sim.stats(), path.read_bytes(), sim.memory_performance()


@pytest.mark.parametrize(
    "memory,storage,admit", [(1, 12, True), (5, 16, True), (11, 4096, True), (7, 24, False)]
)
@pytest.mark.parametrize("groups,workers", [(1, 1), (7, 1), (7, 3)])
def test_indexed_matches_baseline_through_segment_lifecycles(
    tmp_path,
    memory,
    storage,
    admit,
    groups,
    workers,
):
    cfg = configuration(memory, storage, admit)
    requests = list(workload())
    expected = run(tmp_path, "baseline", cfg, requests)
    cfg = replace(
        cfg,
        memory_policy=replace(
            cfg.memory_policy, kind="indexed_lru", groups=groups, workers=workers, profile=True
        ),
    )
    actual = run(tmp_path, "indexed", cfg, requests)
    assert actual[:2] == expected[:2]
    assert actual[2]["decisions"] > 0
    assert (actual[2]["worker_rounds"] > 0) == (workers > 1)


@pytest.mark.parametrize(
    "storage_kind",
    ["baseline_ratio_lru", "wear_share_round_robin", "wear_share_affinity", "adaptive_endurance"],
)
def test_indexed_preserves_existing_storage_algorithms(tmp_path, storage_kind):
    cfg = replace(
        configuration(7, 24),
        storage_policy=StoragePolicyConfig(
            kind=storage_kind, slc_write_ratio=0.5, background_period_ns=5
        ),
    )
    requests = list(workload(count=150))
    expected = run(tmp_path, "baseline", cfg, requests)
    actual = run(
        tmp_path,
        "indexed",
        replace(cfg, memory_policy=MemoryPolicyConfig(kind="indexed_lru", groups=5)),
        requests,
    )
    assert actual[:2] == expected[:2]


def test_sampled_is_deterministic_across_workers(tmp_path):
    cfg = replace(
        configuration(11, 24),
        memory_policy=MemoryPolicyConfig(kind="indexed_lru", groups=9, sampled_groups=2, seed=42),
    )
    requests = list(workload())
    first = run(tmp_path, "one", cfg, requests)
    second = run(
        tmp_path,
        "many",
        replace(cfg, memory_policy=replace(cfg.memory_policy, workers=3)),
        requests,
    )
    assert first[:2] == second[:2]


def write_parquet(path, requests):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    schema = pa.schema(
        [(name, pa.uint64()) for name in ("timestamp_ns", "request_id", "affinity_id")]
        + [("hash_ids", pa.list_(pa.uint64()))]
    )
    columns = dict(zip(schema.names, zip(*requests)))
    pq.write_table(pa.table(columns, schema=schema), path, row_group_size=13)


@pytest.mark.parametrize("prefetch", [False, True])
@pytest.mark.parametrize("kind", ["indexed_lru", "context_lru"])
def test_columnar_queue_matches_row_replay_and_bounds_buffers(tmp_path, prefetch, kind):
    requests = list(workload(count=113))
    paths = [tmp_path / "a.parquet", tmp_path / "b.parquet"]
    write_parquet(paths[0], requests[:55])
    write_parquet(paths[1], requests[55:])
    cfg = replace(configuration(7, 24), memory_policy=MemoryPolicyConfig(kind=kind, alpha=0.5))
    expected = run(tmp_path, "rows", cfg, requests)
    input_cfg = InputConfig(
        batch_requests=9,
        batch_hashes=11,
        max_request_hashes=16,
        queue_batches=2,
        inflight_bytes=2048,
        prefetch=prefetch,
    )
    path = tmp_path / "batch.csv"
    sim = DWPDSimulator(cfg, path)
    timings = replay_batches(sim, parquet_batches(paths, input_cfg), input_cfg)
    assert (sim.stats(), path.read_bytes()) == expected[:2]
    assert timings["peak_reserved_bytes"] <= input_cfg.inflight_bytes
    assert timings["peak_inflight_batches"] <= input_cfg.queue_batches


@pytest.mark.parametrize("prefetch", [False, True])
def test_input_errors_propagate_without_hanging(tmp_path, prefetch):
    path = tmp_path / "bad.parquet"
    write_parquet(path, [(3, 0, 0, [1]), (2, 1, 0, [2])])
    cfg = InputConfig(batch_requests=1, prefetch=prefetch)
    sim = DWPDSimulator(configuration(2, 4), tmp_path / "bad.csv")
    with pytest.raises(ValueError, match="nondecreasing"):
        replay_batches(sim, parquet_batches([path], cfg), cfg)
    # Consumer failure while a producer can be waiting on a full queue.
    write_parquet(path, [(i, 0, 0, [i]) for i in range(30)])
    sim = DWPDSimulator(configuration(2, 4), tmp_path / "duplicate.csv")
    with pytest.raises(ValueError, match="unique"):
        replay_batches(sim, parquet_batches([path], cfg), cfg)


@pytest.mark.parametrize("streaming", [False, True])
def test_huggingface_dataset_preserves_uint64_paths(tmp_path, streaming):
    datasets = pytest.importorskip("datasets")
    from dwpdsim import huggingface_batches

    requests = [
        (0, 0, 0, [2**63 + 1, 2**64 - 1]),
        (0, 1, 0, [2**63 + 1]),
        (1, 2, 0, []),
        (2, 3, 0, [99]),
    ]
    features = datasets.Features(
        {
            "timestamp_ns": datasets.Value("uint64"),
            "request_id": datasets.Value("uint64"),
            "affinity_id": datasets.Value("uint64"),
            "hash_ids": datasets.List(datasets.Value("uint64")),
            "unused": datasets.Value("string"),
        }
    )
    columns = dict(zip(("timestamp_ns", "request_id", "affinity_id", "hash_ids"), zip(*requests)))
    columns["unused"] = ["ignored"] * len(requests)
    import pyarrow as pa

    dataset = datasets.Dataset(pa.table(columns, schema=features.arrow_schema))
    if streaming:
        dataset = dataset.to_iterable_dataset()
    cfg = configuration(1, 8)
    expected = run(tmp_path, "rows", cfg, requests)
    target = tmp_path / "hf.csv"
    sim = DWPDSimulator(cfg, target)
    inputs = InputConfig(batch_requests=2, prefetch=True)
    replay_batches(sim, huggingface_batches(dataset, inputs), inputs)
    assert (sim.stats(), target.read_bytes()) == expected[:2]


@pytest.mark.parametrize("prefetch", [False, True])
def test_oversize_request_is_rejected_and_singleton_is_not_split(tmp_path, prefetch):
    path = tmp_path / "long.parquet"
    requests = [(0, 0, 0, list(range(20))), (1, 1, 0, [])]
    write_parquet(path, requests)
    cfg = InputConfig(
        batch_requests=4,
        batch_hashes=5,
        max_request_hashes=24,
        queue_batches=1,
        inflight_bytes=512,
        prefetch=prefetch,
    )
    expected = run(tmp_path, "rows", configuration(24, 32), requests)
    sim = DWPDSimulator(configuration(24, 32), tmp_path / "batch-long.csv")
    replay_batches(sim, parquet_batches([path], cfg), cfg)
    assert (sim.stats(), (tmp_path / "batch-long.csv").read_bytes()) == expected[:2]
    cfg = replace(cfg, max_request_hashes=10)
    with pytest.raises(ValueError, match="max_request_hashes"):
        list(parquet_batches([path], cfg))


@pytest.mark.parametrize("workers,sampled_groups", [(1, 0), (3, 0), (3, 2)])
@pytest.mark.parametrize(
    "retention_ns,evict_ns,dropped",
    [
        (None, 11, False),
        (10, 9, False),
        (10, 10, False),
        (10, 11, True),
        (0, 0, False),
        (0, 1, True),
    ],
)
def test_indexed_retention_strict_boundary(
    tmp_path, workers, sampled_groups, retention_ns, evict_ns, dropped
):
    cfg = replace(
        configuration(1, 8),
        memory_policy=MemoryPolicyConfig(
            kind="indexed_lru",
            groups=7,
            workers=workers,
            sampled_groups=sampled_groups,
            retention_ns=retention_ns,
        ),
    )
    stats, _, _ = run(tmp_path, "retention", cfg, [(0, 0, 0, [1]), (evict_ns, 1, 0, [2])])
    assert stats["memory"]["drop_blocks"] == int(dropped)
    assert stats["memory"]["dump_blocks"] == int(not dropped)
    assert stats["trace"]["events"] == int(not dropped)


@pytest.mark.parametrize("evict_ns,dropped", [(11, False), (19, False), (20, True)])
def test_retention_uses_newest_memory_member_not_endpoint(tmp_path, evict_ns, dropped):
    cfg = replace(
        configuration(2, 8), memory_policy=MemoryPolicyConfig(kind="indexed_lru", retention_ns=10)
    )
    stats, _, _ = run(
        tmp_path,
        "refresh",
        cfg,
        [
            (0, 0, 0, [1, 2]),
            (9, 1, 0, [1]),
            (evict_ns, 2, 0, [3]),
        ],
    )
    assert stats["memory"]["drop_blocks"] == 2 * int(dropped)
    assert stats["memory"]["dump_blocks"] == 2 * int(not dropped)


def test_retention_tracks_split_merge_and_preserves_hot_prefix(tmp_path):
    cfg = replace(
        configuration(4, 16),
        memory_policy=MemoryPolicyConfig(kind="indexed_lru", groups=7, workers=3, retention_ns=5),
    )
    stats, trace, _ = run(
        tmp_path,
        "split-merge",
        cfg,
        [
            (0, 0, 0, [1, 2, 3, 4]),
            (9, 1, 0, [1, 2, 5]),
            (10, 2, 0, [6]),
            (11, 3, 0, [7]),
        ],
    )
    assert stats["memory"]["drop_blocks"] == 2
    assert stats["memory"]["dump_blocks"] == 3
    assert stats["tree"]["nodes_removed"] == 2
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(trace.decode())))
    assert [(row["operation"], row["hash_id"]) for row in rows] == [
        ("WRITE", "1"),
        ("WRITE", "2"),
        ("WRITE", "5"),
    ]


def test_retention_drop_keeps_storage_copy(tmp_path):
    cfg = replace(
        configuration(1, 8), memory_policy=MemoryPolicyConfig(kind="indexed_lru", retention_ns=10)
    )
    stats, trace, _ = run(
        tmp_path,
        "storage-copy",
        cfg,
        [
            (0, 0, 0, [1]),
            (1, 1, 0, [2]),
            (2, 2, 0, [1]),
            (20, 3, 0, [3]),
            (21, 4, 0, [1]),
        ],
    )
    assert stats["memory"]["drop_blocks"] == 1
    assert stats["memory"]["dump_blocks"] == 3
    assert stats["accesses"]["tlc_hits"] == 2
    assert b"TRIM" not in trace


def test_retention_is_checked_only_when_selected_for_eviction(tmp_path):
    cfg = replace(
        configuration(4, 8),
        simulation_end_ns=100,
        memory_policy=MemoryPolicyConfig(kind="indexed_lru", retention_ns=5),
    )
    stats, _, _ = run(tmp_path, "no-pressure", cfg, [(0, 0, 0, [1])])
    assert stats["memory"]["resident_blocks"] == 1
    assert stats["memory"]["drop_blocks"] == 0
    assert stats["trace"]["events"] == 0


def test_retention_is_not_silently_accepted_by_baseline(tmp_path):
    cfg = replace(configuration(1, 8), memory_policy=MemoryPolicyConfig(retention_ns=10))
    with pytest.raises(ValueError, match="retention"):
        DWPDSimulator(cfg, tmp_path / "baseline-retention.csv")


@pytest.mark.parametrize(
    "alpha,expected",
    [(0.01, [1, 2, 3]), (0.6, [1, 2, 3]), (0.61, [4]), (0.8, [4]), (1.0, [4])],
)
def test_context_lru_capacity_budget_and_recency_tie(tmp_path, alpha, expected):
    cfg = replace(
        configuration(5, 32),
        memory_policy=MemoryPolicyConfig(kind="context_lru", alpha=alpha),
    )
    _, trace, _ = run(tmp_path, "context-budget", cfg, [
        (0, 0, 0, [1, 2, 3]), (1, 1, 0, [4]), (2, 2, 0, [5]), (3, 3, 0, [6]),
    ])
    rows = list(csv.DictReader(io.StringIO(trace.decode())))
    assert [(r["operation"], int(r["hash_id"])) for r in rows] == [
        ("WRITE", node) for node in expected
    ]


def test_context_lru_uses_depth_before_segment_size(tmp_path):
    cfg = replace(
        configuration(7, 32),
        memory_policy=MemoryPolicyConfig(kind="context_lru", alpha=1),
    )
    # The two one-block suffixes have depth 4; the later two-block root segment has depth 2.
    _, trace, _ = run(tmp_path, "context-depth", cfg, [
        (0, 0, 0, [1, 2, 3, 4]), (1, 1, 0, [1, 2, 3, 5]),
        (2, 2, 0, [6, 7]), (3, 3, 0, [8]),
    ])
    rows = list(csv.DictReader(io.StringIO(trace.decode())))
    assert [int(r["hash_id"]) for r in rows] == [6, 7]


def test_context_lru_equal_depth_prefers_fewer_residents(tmp_path):
    cfg = replace(
        configuration(7, 32),
        memory_policy=MemoryPolicyConfig(kind="context_lru", alpha=1),
    )
    _, trace, _ = run(tmp_path, "context-size", cfg, [
        (0, 0, 0, [1, 2, 3]), (1, 1, 0, [4, 5, 6]),
        (2, 2, 0, [4, 5, 7]), (3, 3, 0, [8]),
    ])
    rows = list(csv.DictReader(io.StringIO(trace.decode())))
    assert [int(r["hash_id"]) for r in rows] == [6]


def test_context_lru_dump_stops_at_selected_segment(tmp_path):
    cfg = replace(configuration(4, 32), memory_policy=MemoryPolicyConfig(
        kind="context_lru", alpha=0.01,
    ))
    with DWPDSimulator(cfg, tmp_path / "current-segment.csv") as sim:
        sim.process(0, 0, 0, [1, 2, 3, 4])
        sim.process(1, 1, 0, [5])  # Persist the original path.
        sim.process(2, 2, 0, [1, 2, 3, 4])  # Admit it back into Memory.
        before = sim.stats()["memory"]["evicted_blocks"]
        sim.process(3, 3, 0, [1, 2, 6])  # Split at 2 and evict the stored suffix [3, 4].
        assert sim.stats()["memory"]["evicted_blocks"] - before == 2
        assert sim.stats()["memory"]["resident_blocks"] == 3
        hits = sim.stats()["accesses"]["memory_hits"]
        sim.process(4, 4, 0, [1, 2])
        assert sim.stats()["accesses"]["memory_hits"] - hits == 2


@pytest.mark.parametrize("storage", ["baseline_fixed_lru", "infinite_storage",
                                     "wear_share_round_robin", "wear_share_affinity",
                                     "adaptive_endurance"])
@pytest.mark.parametrize("retention", [None, 0])
@pytest.mark.parametrize("cap,removed", [(1, 1), (2, 2), (8, 4), (None, 4)])
def test_context_lru_caps_dump_and_drop_from_tail(tmp_path, storage, retention, cap, removed):
    cfg = replace(configuration(4, 32),
                  memory_policy=MemoryPolicyConfig(kind="context_lru", alpha=1,
                                                  retention_ns=retention,
                                                  max_eviction_blocks=cap),
                  storage_policy=StoragePolicyConfig(kind=storage))
    with DWPDSimulator(cfg, tmp_path / "capped.csv") as sim:
        sim.process(0, 0, 0, [1, 2, 3, 4])
        sim.process(1, 1, 0, [5])
        stats = sim.stats()
        assert stats["memory"]["evicted_blocks"] == removed
        assert stats["memory"]["resident_blocks"] == 5 - removed
        action = "drop_blocks" if retention == 0 else "dump_blocks"
        assert stats["memory"][action] == removed
        # The surviving prefix must still hit; the missing suffix counts as misses only for Drop.
        sim.process(2, 2, 0, list(range(1, 5 - removed)))
        assert sim.stats()["accesses"]["memory_hits"] == 4 - removed
        sim.process(3, 3, 0, [1, 2, 3, 4])
        assert sim.stats()["accesses"]["compute_cost"] == 17 + (4 * removed if retention == 0 else 0)


@pytest.mark.parametrize("alpha", [0, -0.1, 1.1, float("nan"), float("inf")])
def test_context_lru_rejects_invalid_alpha(tmp_path, alpha):
    cfg = replace(configuration(5, 32), memory_policy=MemoryPolicyConfig(
        kind="context_lru", alpha=alpha,
    ))
    with pytest.raises(ValueError, match="alpha"):
        DWPDSimulator(cfg, tmp_path / "invalid-alpha.csv")


@pytest.mark.parametrize("options", [{"groups": 2}, {"sampled_groups": 1}, {"workers": 2}])
def test_context_lru_requires_global_exact_index(tmp_path, options):
    cfg = replace(configuration(5, 32), memory_policy=MemoryPolicyConfig(
        kind="context_lru", **options,
    ))
    with pytest.raises(ValueError, match="context_lru requires"):
        DWPDSimulator(cfg, tmp_path / "invalid-context-index.csv")
