"""Audit the four completed baseline cases against native and device records."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def summarize(root: Path, require_complete: bool = False) -> dict:
    rows = []
    for capacity in (1, 4):
        for policy in ("rr", "adaptive"):
            name = f"{policy}-{capacity}tb-1000"
            directory = root / name
            native = json.loads((directory / "result.json").read_text())
            manifest = json.loads((directory / "device/manifest.json").read_text())
            hardware = ET.parse(directory / "device/ssd.xml").getroot()
            for key, expected in {"Flash_Channel_Count": 6, "Chip_No_Per_Channel": 2,
                                  "Die_No_Per_Chip": 2, "Plane_No_Per_Die": 4,
                                  "Page_No_Per_Block": 4224, "Page_Capacity": 16384,
                                  "Block_No_Per_Plane": {1: 173, 4: 689}[capacity]}.items():
                assert int(hardware.findtext(".//" + key)) == expected
            assert float(hardware.findtext(".//Overprovisioning_Ratio")) == 0.125
            for pool in hardware.findall(".//Flash_Pool_Parameter_Set"):
                pool_name = pool.findtext("Pool_ID")
                assert pool.findtext("Channel_IDs") == {"slc": "0,1", "tlc": "2,3,4,5"}[pool_name]
                assert int(pool.findtext("Logical_Capacity_In_Sectors")) * 512 == native["config"][pool_name]["capacity_bytes"]
            for profile in hardware.findall(".//Flash_Media_Profile"):
                assert int(profile.findtext("Block_PE_Cycles_Limit")) == {"SLC": 3, "TLC": 7}[profile.findtext("Flash_Technology")]
            stats = native["stats"]
            prefill = stats["prefill"]
            assert native["replayed_requests"] == 1000 and not native["full_corpus"]
            assert prefill["input_tokens"] == 39_720_064
            assert prefill["restored_tokens"] + prefill["prefill_compute_tokens"] == prefill["input_tokens"]
            assert stats["full"]["demands"] * 64 == prefill["restored_tokens"]
            assert stats["linear"]["demands"] == prefill["restored_requests"]
            assert sum(stats[c]["write_allocated_bytes"] for c in ("full", "linear")) == sum(
                stats["storage"][p]["write_bytes"] for p in ("slc", "tlc"))
            row = {
                "case": name, "capacity_tb": capacity, "policy": policy,
                "requests": native["replayed_requests"], "input_tokens": prefill["input_tokens"],
                "restored_tokens": prefill["restored_tokens"], "restored_requests": prefill["restored_requests"],
                "joint_token_hit_rate": prefill["joint_token_hit_rate"],
                "last_request_timestamp_ns": native["last_request_timestamp_ns"],
                "background_ticks": stats["storage"]["background_ticks"],
                "native_seconds": native["replay_seconds"],
                "native_binary_sha256": native["native_extension_sha256"],
                "payload_write_bytes": sum(stats[c]["write_bytes"] for c in ("full", "linear")),
                "host_write_bytes": sum(stats["storage"][p]["write_bytes"] for p in ("slc", "tlc")),
                "commands": manifest["commands"], "pools": {},
            }
            result_path = directory / "device/device_result.json"
            if not result_path.exists():
                row["status"] = "pending"
            else:
                device = json.loads(result_path.read_text())
                row.update(status=device["status"], device_seconds=device["seconds"],
                           device_binary_sha256=device["binary_sha256"])
                if device["status"] == "completed":
                    xml = ET.parse(directory / "device/workload_scenario_1.xml").getroot()
                    flows = {int(f.findtext("Flow_ID")): f for f in xml.findall("Host/Host.IO_Flow")}
                    assert set(flows) == set(range(len(manifest["flows"])))
                    for flow_id, expected in enumerate(manifest["flows"]):
                        flow = flows[flow_id]
                        count = sum(v for k, v in expected["counts"].items() if k.endswith("_commands"))
                        assert flow.findtext("Pool_ID") == expected["pool"]
                        assert int(flow.findtext("Generated_Request_Count")) == count
                        assert int(flow.findtext("Completed_Request_Count")) == count
                    pools = {p.get("ID"): p for p in xml.findall(".//SSDDevice.Pool")}
                    assert set(pools) == {"slc", "tlc"}
                    for pool, element in pools.items():
                        def value(key, element=element):
                            return int(element.get(key))
                        for op, field in (("WRITE", "Host_Write_Bytes"), ("READ", "Host_Read_Bytes"),
                                          ("TRIM", "Requested_Trim_Bytes")):
                            expected = stats["storage"][pool][op.lower() + "_bytes"]
                            assert value(field) == expected == manifest["pools"][pool].get(op + "_bytes", 0)
                        host = value("Measurement_Host_Write_Bytes")
                        nand = value("Measurement_Flash_Programmed_Bytes")
                        gc = value("GC_Page_Program_Count")
                        assert host == value("Host_Write_Bytes")
                        assert nand == value("Flash_Program_Command_Count") * 16384
                        assert nand == host + gc * 16384
                        assert value("Effective_Trimmed_Bytes") <= value("Requested_Trim_Bytes")
                        row["pools"][pool] = {
                            "host_read_bytes": value("Host_Read_Bytes"), "host_write_bytes": host,
                            "requested_trim_bytes": value("Requested_Trim_Bytes"),
                            "effective_trim_bytes": value("Effective_Trimmed_Bytes"),
                            "nand_program_bytes": nand, "waf": nand / host,
                            "gc_program_pages": gc, "gc_executions": value("GC_Execution_Count"),
                            "total_block_erases": value("Total_Block_Erase_Count"),
                            "max_block_erases": value("Max_Block_Erase_Count"),
                            "pe_cycle_limit": value("PE_Cycle_Limit"),
                            "logical_capacity_bytes": value("Logical_Capacity_Bytes"),
                            "physical_capacity_bytes": value("Physical_Capacity_Bytes"),
                        }
                        flow = next(f for f in flows.values() if f.findtext("Pool_ID") == pool)
                        row["pools"][pool].update(
                            average_read_response_ns=int(flow.findtext("Average_Response_Time_Read")),
                            average_write_response_ns=int(flow.findtext("Average_Response_Time_Write")),
                        )
                    row["nand_program_bytes"] = sum(p["nand_program_bytes"] for p in row["pools"].values())
                    row["aggregate_waf"] = row["nand_program_bytes"] / row["host_write_bytes"]
                    row["completed_commands"] = sum(int(f.findtext("Completed_Request_Count")) for f in flows.values())
                    assert row["completed_commands"] == row["commands"]
            if require_complete:
                assert row["status"] == "completed", (name, row["status"])
            rows.append(row)
    return {"scope": "cold WEKA prefix of 1000 requests; all device I/O drained for completed cases",
            "units": {"bytes": "bytes", "time": "*_ns is nanoseconds; *_seconds is seconds"}, "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("build/weka-storage-baseline"))
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    result = summarize(args.root, args.require_complete)
    (args.root / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    for row in result["cases"]:
        print(row["case"], row["status"], f"joint={row['joint_token_hit_rate']:.6%}",
              f"WAF={row.get('aggregate_waf', 'pending')}")


if __name__ == "__main__":
    main()
