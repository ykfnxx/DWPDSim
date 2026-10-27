"""Finite pools retain independent component availability and causal migration."""

import csv
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
from dwpdsim.hybrid_mqsim import convert_hybrid_trace


def configuration(policy, trace=True):
    return SimulationConfig(
        memory=MemoryConfig(32), slc=StorageTierConfig(128, 1),
        tlc=StorageTierConfig(256, 1), block_size_bytes=8,
        storage_policy=StoragePolicyConfig(
            kind=policy, logical_fill_fraction=1, slc_host_share=1/3,
            promotion_seconds=1, idle_multiplier=0.001, background_period_ns=1_000_000_000,
        ),
        hybrid_attention=HybridAttentionConfig(
            pool_allocation="fixed",
            enabled=True, full_capacity_bytes=16, linear_capacity_bytes=16,
            full_block_bytes=8, linear_checkpoint_bytes=16,
            full_policy="lru_k", linear_policy="lru_k", replay_mode="prefix_replay",
            tokens_per_block=2, checkpoint_interval_tokens=2,
            checkpoint_generation="request_end", emit_io_trace=trace,
        ),
    )


@pytest.mark.parametrize("policy", ["wear_share_round_robin", "adaptive_endurance"])
def test_finite_component_replay_trace_and_no_trace_agree(tmp_path, policy):
    results = []
    for trace in (True, False):
        path = tmp_path / str(trace)
        with DWPDSimulator(configuration(policy, trace), path) as sim:
            for i in range(100):
                session = i % 3
                hashes = [session * 100 + j for j in range(1, 4 + i // 9)]
                sim.process_hybrid_prefill(HybridPrefillRequest(
                    i * 1_000_000_000, i, i, hashes, input_tokens=2*len(hashes),
                    affinity_id=session + 1,
                ))
            stats = sim.hybrid_stats()
        results.append(stats)
        output = path.with_name(path.name + ".hybrid.csv")
        assert output.exists() == trace
        if not trace:
            continue
        live, objects = {}, {}
        rows = list(csv.DictReader(output.open()))
        for sequence, row in enumerate(rows):
            assert int(row["sequence"]) == sequence
            key = (row["component"], int(row["hash_id"]))
            addresses = [int(v) for v in row["storage_addresses"].split(";")]
            assert int(row["allocated_bytes"]) == len(addresses)*8
            pool = row["storage_tier"]
            assert max(addresses)*8 < {"slc": 128, "tlc": 256}[pool]
            if row["operation"] == "WRITE":
                for address in addresses:
                    assert (pool, address) not in live
                    live[pool, address] = key
                objects[pool, key] = addresses
            else:
                assert objects[pool, key] == addresses
                assert all(live[pool, address] == key for address in addresses)
                if row["operation"] == "TRIM":
                    for address in addresses:
                        del live[pool, address]
                    del objects[pool, key]
            if row["depends_on_sequence"]:
                assert int(row["depends_on_sequence"]) < sequence
        for pool in ("slc", "tlc"):
            counts = stats["storage"][pool]
            assert counts["used_bytes"] == 8*sum(k[0] == pool for k in live)
            assert counts["peak_used_bytes"] <= counts["capacity_bytes"]
        assert stats["full"]["trim_objects"] > 0
        assert stats["prefill"]["restored_tokens"] == stats["full"]["demands"]*2
        assert stats["linear"]["demands"] == stats["prefill"]["restored_requests"]
    assert results[0] == results[1]
    if policy == "adaptive_endurance":
        assert results[0]["storage"]["gap_samples"] == 97


def test_adaptive_migration_then_idle_trim_cannot_create_joint_hit(tmp_path):
    cfg = configuration("adaptive_endurance")
    cfg = replace(cfg, hybrid_attention=replace(
        cfg.hybrid_attention, full_capacity_bytes=8, linear_capacity_bytes=24,
        replay_mode="exact_objects", tokens_per_block=0,
        checkpoint_interval_tokens=4096, checkpoint_generation="chunked_prefill",
    ))
    path = tmp_path / "adaptive"
    with DWPDSimulator(cfg, path) as sim:
        sim.process_hybrid(HybridEvent(0, 0, "publish", [11], components="full"))
        sim.process_hybrid(HybridEvent(0, 1, "publish", [22], components="full"))
        sim.process_hybrid(HybridEvent(2_000_000_000, 2, "observe", [22]))
        assert sim.hybrid_inspect(11, "full")["storage_tier"] == "tlc"
        sim.process_hybrid(HybridEvent(61_000_000_000, 3, "observe", [22]))
        assert not sim.hybrid_inspect(11, "full")["on_storage"]
        result = sim.process_hybrid(HybridEvent(61_000_000_000, 4, "request", [11], request_id=1))
        assert not result["all_available"]
        assert result["full"] == ["lost"] and result["linear"] == "never_published"
    rows = list(csv.DictReader(path.with_name(path.name + ".hybrid.csv").open()))
    move = [r for r in rows if r["reason"] == "BACKGROUND_MIGRATION"]
    assert [r["operation"] for r in move] == ["READ", "WRITE", "TRIM"]
    assert move[1]["depends_on_sequence"] == move[0]["sequence"]
    assert move[2]["depends_on_sequence"] == move[1]["sequence"]
    assert rows[-1]["reason"] == "IDLE_EVICTION"


def test_streaming_converter_preserves_allocated_io_and_cross_flow_order(tmp_path):
    from dataclasses import asdict

    cfg = configuration("wear_share_round_robin")
    cfg = replace(cfg, memory=MemoryConfig(16384), block_size_bytes=4096,
                  slc=StorageTierConfig(65536, 2), tlc=StorageTierConfig(131072, 2),
                  hybrid_attention=replace(cfg.hybrid_attention,
                      full_capacity_bytes=8192, linear_capacity_bytes=8192,
                      full_block_bytes=4096, linear_checkpoint_bytes=8192))
    path = tmp_path / "objects"
    with DWPDSimulator(cfg, path) as sim:
        for i in range(100):
            hashes = [(i % 3)*100+j for j in range(1, 4+i//9)]
            sim.process_hybrid_prefill(HybridPrefillRequest(i*10**9, i, i, hashes,
                                                         input_tokens=2*len(hashes)))
        stats = sim.hybrid_stats()
    manifest = convert_hybrid_trace(path.with_name(path.name+".hybrid.csv"), asdict(cfg), tmp_path/"device")
    requests = {}
    for flow in manifest["flows"]:
        with open(flow["path"]) as handle:
            for line in handle:
                time, _, lba, length, operation, request, dependencies = line.split()
                request = int(request)
                assert request not in requests
                assert 0 < int(length) <= 65535
                assert 0 <= int(lba)*512 < getattr(cfg, flow["pool"]).capacity_bytes
                requests[request] = (int(time), int(operation), dependencies)
    assert set(requests) == set(range(manifest["commands"]))
    assert manifest["source_events"] == stats["io_events"]
    for request, (_, _, dependencies) in requests.items():
        if dependencies != "-1":
            assert all(int(value) < request and int(value) in requests for value in dependencies.split(","))
    for pool in ("slc", "tlc"):
        for operation in ("read", "write", "trim"):
            assert manifest["pools"][pool][operation.upper()+"_bytes"] == stats["storage"][pool][operation+"_bytes"]


def test_rejected_oversized_checkpoint_cannot_count_as_joint_hit(tmp_path):
    cfg = configuration("wear_share_round_robin")
    cfg = replace(cfg, memory=MemoryConfig(8192), block_size_bytes=4096,
                  slc=StorageTierConfig(8192, 1), tlc=StorageTierConfig(8192, 1),
                  hybrid_attention=replace(cfg.hybrid_attention,
                      full_capacity_bytes=4096, linear_capacity_bytes=4096,
                      full_block_bytes=4096, linear_checkpoint_bytes=12288))
    with DWPDSimulator(cfg, tmp_path/"rejected") as sim:
        sim.process_hybrid_prefill(HybridPrefillRequest(0, 0, 0, [1, 2], input_tokens=4))
        assert not sim.hybrid_inspect(2, "linear")["on_storage"]
        result = sim.process_hybrid_prefill(HybridPrefillRequest(1, 1, 1, [1, 2, 3], input_tokens=6))
        assert result["restore_tokens"] == 0
        assert sim.hybrid_stats()["linear"]["admission_rejections"] == 2
