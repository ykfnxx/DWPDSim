"""Stream finite hybrid object I/O into the patched MQSim dependency format.

Device transfers include the complete allocated units, including padding. State
tracks allocation-unit hazards across streams; no full command list is retained.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import resource
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import ExitStack
from pathlib import Path


def convert_hybrid_trace(trace: Path, config: dict, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    unit = config["block_size_bytes"]
    assert unit % 512 == 0 and unit <= 65_535 * 512
    units_per_command = 65_535 * 512 // unit
    capacities = {pool: config[pool]["capacity_bytes"] for pool in ("slc", "tlc")}
    counts = {pool: config[pool]["stream_count"] for pool in capacities}
    flows = [(pool, stream) for pool in capacities for stream in range(counts[pool])]
    commands, events, previous_time = 0, 0, -1
    final_by_event = {}
    # (last mutation, readers since mutation); keys are pool-global allocation units.
    hazards = {}
    totals = {pool: Counter() for pool in capacities}
    flow_totals = {key: Counter() for key in flows}
    digest = hashlib.sha256()
    with ExitStack() as stack:
        handles = {key: stack.enter_context((output / f"{key[0]}-{key[1]}.trace").open("w")) for key in flows}
        source = stack.enter_context(trace.open())
        reader = csv.DictReader(source)
        required = {"sequence", "timestamp_ns", "component", "hash_id", "operation", "payload_bytes",
                    "allocated_bytes", "storage_addresses", "storage_tier", "stream_id", "reason",
                    "move_id", "depends_on_sequence"}
        if set(reader.fieldnames or ()) != required:
            raise ValueError("MQSim conversion requires finite hybrid object trace v2")
        for row in reader:
            timestamp = int(row["timestamp_ns"])
            assert timestamp >= previous_time
            if timestamp != previous_time:
                final_by_event.clear()
                previous_time = timestamp
            sequence = int(row["sequence"])
            assert sequence == events
            events += 1
            pool, stream = row["storage_tier"], int(row["stream_id"])
            key = pool, stream
            addresses = [int(value) for value in row["storage_addresses"].split(";")]
            assert len(addresses) * unit == int(row["allocated_bytes"])
            assert addresses and min(addresses) >= 0 and (max(addresses)+1)*unit <= capacities[pool]
            operation = row["operation"]
            operation_code = {"WRITE": 0, "READ": 1, "TRIM": 2}[operation]
            explicit = row["depends_on_sequence"]
            predecessor = final_by_event[int(explicit)] if explicit else None
            previous_chunk = None
            start = 0
            while start < len(addresses):
                end = start + 1
                while (end < len(addresses) and end-start < units_per_command
                       and addresses[end] == addresses[end-1]+1):
                    end += 1
                deps = set()
                for address in addresses[start:end]:
                    state = hazards.setdefault((pool, address), [None, set()])
                    if state[0] is not None:
                        deps.add(state[0])
                    if operation == "READ":
                        state[1].add(commands)
                    else:
                        deps.update(state[1])
                        state[1].clear()
                        state[0] = commands
                if predecessor is not None:
                    deps.add(predecessor)
                if previous_chunk is not None:
                    deps.add(previous_chunk)
                assert all(dependency < commands for dependency in deps)
                dependency_text = ",".join(str(value) for value in sorted(deps)) if deps else "-1"
                length = (end-start)*unit
                line = f"{timestamp} 0 {addresses[start]*unit//512} {length//512} {operation_code} {commands} {dependency_text}\n"
                handles[key].write(line)
                digest.update(line.encode())
                for counter in (totals[pool], flow_totals[key]):
                    counter[operation + "_commands"] += 1
                    counter[operation + "_bytes"] += length
                previous_chunk = commands
                commands += 1
                start = end
            final_by_event[sequence] = previous_chunk
    manifest = {
        "source": str(trace.resolve()), "source_events": events, "commands": commands,
        "global_emission_sha256": digest.hexdigest(),
        "transfer_scope": "entire allocated units, including padding; pool-global hazards across streams",
        "converter_state": "allocation-unit hazards/readers since last mutation and event IDs at the current timestamp",
        "pools": {pool: dict(values) for pool, values in totals.items()},
        "flows": [{"pool": pool, "stream": stream, "path": str((output / f"{pool}-{stream}.trace").resolve()),
                   "counts": dict(flow_totals[pool, stream])} for pool, stream in flows],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    return manifest


def write_device_configuration(template: Path, output: Path, config: dict, capacity_tb: int):
    tree = ET.parse(template)
    settings = {
        "Flash_Channel_Count": 6, "Chip_No_Per_Channel": 2,
        "Die_No_Per_Chip": 2, "Plane_No_Per_Die": 4,
        "Page_No_Per_Block": 4224, "Page_Capacity": 16384,
        "Block_No_Per_Plane": {1: 173, 4: 689}[capacity_tb],
        "Overprovisioning_Ratio": 0.125,
        "Ideal_Mapping_Table": "true", "CMT_Capacity": 2097152,
        "IO_Queue_Depth": 64, "Queue_Fetch_Size": 16,
        "Measurement_Start_Time_Ns": 0, "Measurement_End_Time_Ns": 10**18,
    }
    for name, value in settings.items():
        tree.find(".//"+name).text = str(value)
    for profile in tree.findall(".//Flash_Media_Profile"):
        slc = profile.findtext("Flash_Technology") == "SLC"
        reads = (75000,)*3 if slc else (75000, 120000, 140000)
        programs = (750000,)*3 if slc else (1500000, 2000000, 2800000)
        for suffix, read, program in zip(("LSB", "CSB", "MSB"), reads, programs, strict=True):
            profile.find("Page_Read_Latency_"+suffix).text = str(read)
            profile.find("Page_Program_Latency_"+suffix).text = str(program)
        profile.find("Block_Erase_Latency").text = "3800000"
        profile.find("Block_PE_Cycles_Limit").text = "3" if slc else "7"
    for pool in tree.findall(".//Flash_Pool_Parameter_Set"):
        name = pool.findtext("Pool_ID")
        pool.find("Channel_IDs").text = "0,1" if name == "slc" else "2,3,4,5"
        pool.find("Logical_Capacity_In_Sectors").text = str(config[name]["capacity_bytes"]//512)
    tree.write(output)


def write_workload(manifest: dict, output: Path):
    root = ET.Element("MQSim_IO_Scenarios")
    scenario = ET.SubElement(root, "IO_Scenario")
    for flow in manifest["flows"]:
        item = ET.SubElement(scenario, "IO_Flow_Parameter_Set_Trace_Based")
        settings = {
            "Priority_Class": "HIGH", "Device_Level_Data_Caching_Mode": "TURNED_OFF",
            "Pool_ID": flow["pool"], "Initial_Occupancy_Percentage": "0",
            "File_Path": flow["path"], "Percentage_To_Be_Executed": "100",
            "Relay_Count": "1", "Time_Unit": "NANOSECOND",
            "Trace_Format": "DWPDSIM_DEPENDENCY_V1", "Enable_Request_Completion_Log": "false",
        }
        for name, value in settings.items():
            ET.SubElement(item, name).text = value
    ET.ElementTree(root).write(output)


def run_device(binary: Path, directory: Path, manifest: dict) -> dict:
    binary_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    started = time.perf_counter()
    log_path = directory / "mqsim.log"
    with log_path.open("w") as log:
        environment = os.environ.copy()
        environment["MQSIM_GMT_DIR"] = str(directory.resolve())
        completed = subprocess.run(
            [str(binary.resolve()), "-i", str((directory/"ssd.xml").resolve()),
             "-w", str((directory/"workload.xml").resolve())],
            check=False, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=environment,
        )
    result = {
        "returncode": completed.returncode, "seconds": time.perf_counter()-started,
        "binary": str(binary.resolve()), "binary_sha256": binary_sha,
        "child_peak_rss_bytes": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024,
        "log": str(log_path.resolve()),
    }
    if completed.returncode:
        result["last_log_lines"] = log_path.read_text().splitlines()[-15:]
        result["status"] = "wear_limit_reached" if "Block PE cycle limit reached" in log_path.read_text() else "failed"
    else:
        assert "Simulation complete." in log_path.read_text()
        root = ET.parse(directory/"workload_scenario_1.xml").getroot()
        reports = {int(row.findtext("Flow_ID")): row for row in root.findall("Host/Host.IO_Flow")}
        assert set(reports) == set(range(len(manifest["flows"])))
        flow_results = []
        for flow_id, flow in enumerate(manifest["flows"]):
            row = reports[flow_id]
            assert row.findtext("Pool_ID") == flow["pool"]
            expected = sum(value for name, value in flow["counts"].items() if name.endswith("_commands"))
            assert int(row.findtext("Completed_Request_Count")) == expected
            flow_results.append({"flow_id": flow_id, "pool": flow["pool"], "completed_commands": expected,
                                 "average_read_response_ns": int(row.findtext("Average_Response_Time_Read")),
                                 "average_write_response_ns": int(row.findtext("Average_Response_Time_Write"))})
        pools = {}
        for row in root.findall(".//SSDDevice.Pool"):
            name = row.get("ID")
            expected = manifest["pools"][name]
            assert int(row.get("Host_Write_Bytes")) == expected.get("WRITE_bytes", 0)
            assert int(row.get("Host_Read_Bytes")) == expected.get("READ_bytes", 0)
            assert int(row.get("Requested_Trim_Bytes")) == expected.get("TRIM_bytes", 0)
            host = int(row.get("Measurement_Host_Write_Bytes"))
            programmed = int(row.get("Measurement_Flash_Programmed_Bytes"))
            pools[name] = dict(row.attrib)
            pools[name]["measured_waf"] = programmed/host if host else None
        result.update(status="completed", pools=pools, flows=flow_results)
    (directory/"device_result.json").write_text(json.dumps(result, indent=2)+"\n")
    return result
