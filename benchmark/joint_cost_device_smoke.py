"""Small paired functional replay through the real MQSim dependency path.

Logical pools deliberately use a small address space inside the reference 1TB
geometry. This verifies conversion/completion, not finite-capacity GC or WAF.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

from dwpdsim import (
    DWPDSimulator,
    HybridAttentionConfig,
    HybridPrefillRequest,
    JointCostConfig,
    MemoryConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
    _core,
)
from dwpdsim.hybrid_mqsim import (
    convert_hybrid_trace,
    run_device,
    write_device_configuration,
    write_workload,
)


def run(output, binary, retention_baselines=False, recompute_weight=1, storage_survival="pressure"):
    output.mkdir(parents=True, exist_ok=False)
    native = Path(_core.__file__)
    native_sha = hashlib.sha256(native.read_bytes()).hexdigest()
    shutil.copy2(native, output / native.name)
    provenance = {
        "native_path": str(native), "native_sha256": native_sha,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    cfg = SimulationConfig(
        memory=MemoryConfig(2 << 20), block_size_bytes=16384,
        slc=StorageTierConfig(16 << 20, 2), tlc=StorageTierConfig(32 << 20, 2),
        storage_policy=StoragePolicyConfig(kind="wear_share_round_robin", slc_host_share=1/3),
        hybrid_attention=HybridAttentionConfig(
            enabled=True, full_block_bytes=65536, linear_checkpoint_bytes=262144,
            full_policy="lru_k", linear_policy="lru_k", replay_mode="prefix_replay",
            tokens_per_block=64, checkpoint_interval_tokens=64,
            checkpoint_generation="request_end", eviction_mode="joint_cost",
            recompute_weight=recompute_weight,
            cost=JointCostConfig(
                horizons_seconds=(1, 3, 8), sample_every=1, samples=4,
                candidates_per_kind=1, scenario_limit=2, wear_weight=0,
                nand_pages_per_block=4224,
                mapping_pages_per_block=0,
                storage_survival=storage_survival,
            ),
        ),
    )
    requests = []
    for i in range(48):
        root, length = i % 3, 4 + i // 6
        requests.append(HybridPrefillRequest(
            i * 2 * 10**9, i, i, [root * 100 + j for j in range(1, length + 1)],
            input_tokens=64 * length + 1, affinity_id=root + 1,
        ))
    input_sha = hashlib.sha256(json.dumps([
        [q.timestamp_ns, q.sequence, q.request_id, q.hash_ids, q.input_tokens] for q in requests
    ]).encode()).hexdigest()
    results = []
    root = Path(__file__).resolve().parents[1]
    policies = ["baseline", "joint_cost"]
    if retention_baselines:
        policies += ["indexed_lru", "context_lru"]
    for name in policies:
        case = output / name
        case.mkdir()
        selected = cfg if name == "joint_cost" else replace(cfg, hybrid_attention=replace(
            cfg.hybrid_attention, eviction_mode="legacy", cost=JointCostConfig(),
        ))
        if name in ("indexed_lru", "context_lru"):
            selected = replace(selected, hybrid_attention=replace(
                selected.hybrid_attention,
                full_policy="indexed_segment_lru" if name == "indexed_lru" else "context_lru",
                linear_policy="checkpoint_lru", eviction_mode="retention", alpha=0.01,
                full_retention_ns=300 * 10**9, linear_retention_ns=300 * 10**9,
            ))
        with DWPDSimulator(selected, case / "io") as sim:
            sim.run_hybrid_prefill(requests)
            sim.finish()
            stats = sim.hybrid_stats()
        device = case / "device"
        manifest = convert_hybrid_trace(case / "io.hybrid.csv", asdict(selected), device)
        for pool in ("slc", "tlc"):
            for operation in ("read", "write", "trim"):
                assert manifest["pools"][pool].get(operation.upper()+"_bytes", 0) == \
                    stats["storage"][pool][operation+"_bytes"]
        write_device_configuration(root / "example/mqsim/ssdconfig.xml", device / "ssd.xml",
                                   asdict(selected), 1)
        write_workload(manifest, device / "workload.xml")
        physical = run_device(binary, device, manifest)
        if physical["status"] != "completed":
            raise RuntimeError(f"MQSim {name} failed: {physical}")
        row = {"policy": name, "input_sha256": input_sha, "config": asdict(selected),
               "stats": stats, "manifest": manifest, "device": physical}
        (case / "result.json").write_text(json.dumps(row, indent=2) + "\n")
        print(json.dumps({"phase": "completed", "policy": name,
                          "requests": stats["requests"], "commands": manifest["commands"]}), flush=True)
        results.append(row)
    assert results[0]["stats"]["prefill"]["input_tokens"] == results[1]["stats"]["prefill"]["input_tokens"]
    if hashlib.sha256(native.read_bytes()).hexdigest() != native_sha:
        raise RuntimeError("native binary changed during MQSim smoke replay")
    summary = {"status": "passed", "scope": "paired functional I/O conversion/completion; no GC/WAF efficacy claim",
               "input_sha256": input_sha, "policies": [r["policy"] for r in results],
               "native_sha256": native_sha, "native_unchanged_during_run": True}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--retention-baselines", action="store_true")
    parser.add_argument("--recompute-weight", type=float, default=1)
    parser.add_argument("--storage-survival", choices=("pressure", "continuation"), default="pressure")
    args = parser.parse_args()
    run(args.output, args.binary, args.retention_baselines, args.recompute_weight, args.storage_survival)
