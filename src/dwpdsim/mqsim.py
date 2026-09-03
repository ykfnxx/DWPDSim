"""Convert DWPDSim traces, run MQSim per SSD tier, and collect results."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

SECTOR_SIZE_BYTES = 512
MAX_MQSIM_FLOWS = 8
OPERATION_CODES = {"WRITE": 0, "READ": 1, "TRIM": 2}
TIMESTAMP_TO_NANOSECONDS = {
    "s": 1_000_000_000,
    "seconds": 1_000_000_000,
    "ms": 1_000_000,
    "milliseconds": 1_000_000,
    "us": 1_000,
    "microseconds": 1_000,
    "ns": 1,
    "nanoseconds": 1,
}


@dataclass
class StreamTrace:
    tier: str
    source_stream_id: int
    path: Path
    block_size_bytes: int
    handle: TextIO
    node_slots: dict[int, int] = field(default_factory=dict)
    free_slots: list[int] = field(default_factory=list)
    next_slot: int = 0
    operation_counts: dict[str, int] = field(
        default_factory=lambda: {"READ": 0, "WRITE": 0, "TRIM": 0}
    )

    @classmethod
    def open(
        cls,
        tier: str,
        source_stream_id: int,
        path: Path,
        block_size_bytes: int,
    ) -> StreamTrace:
        return cls(
            tier=tier,
            source_stream_id=source_stream_id,
            path=path,
            block_size_bytes=block_size_bytes,
            handle=path.open("w", encoding="utf-8"),
        )

    def emit(self, row: dict[str, str], timestamp_ns: int) -> None:
        operation = row["operation"]
        node_id = int(row["node_id"])

        if operation == "WRITE":
            slot = self.free_slots.pop() if self.free_slots else self.next_slot
            if slot == self.next_slot:
                self.next_slot += 1
            self.node_slots[node_id] = slot
        else:
            slot = self.node_slots[node_id]

        start_lba = slot * self.block_size_bytes // SECTOR_SIZE_BYTES
        sector_count = int(row["length_bytes"]) // SECTOR_SIZE_BYTES
        self.handle.write(
            f"{timestamp_ns} 0 {start_lba} {sector_count} {OPERATION_CODES[operation]}\n"
        )
        self.operation_counts[operation] += 1

        if operation == "TRIM":
            del self.node_slots[node_id]
            self.free_slots.append(slot)

    def close(self) -> None:
        self.handle.close()

    def manifest(self, mqsim_flow_id: int) -> dict[str, object]:
        return {
            "dwpdsim_stream_id": self.source_stream_id,
            "mqsim_flow_id": mqsim_flow_id,
            "trace": str(self.path.resolve()),
            "events": sum(self.operation_counts.values()),
            "operations": self.operation_counts,
            "required_capacity_bytes": self.next_slot * self.block_size_bytes,
        }


def _config_value(root: ET.Element, name: str) -> int:
    return int(root.findtext(f".//{name}"))


def _config_resources(config_path: Path) -> tuple[dict[str, str], int, int, float]:
    root = ET.parse(config_path).getroot()
    counts = {
        "Channel_IDs": _config_value(root, "Flash_Channel_Count"),
        "Chip_IDs": _config_value(root, "Chip_No_Per_Channel"),
        "Die_IDs": _config_value(root, "Die_No_Per_Chip"),
        "Plane_IDs": _config_value(root, "Plane_No_Per_Die"),
    }
    resources = {name: ",".join(str(index) for index in range(count)) for name, count in counts.items()}
    resource_count = 1
    for count in counts.values():
        resource_count *= count

    sectors_per_page = _config_value(root, "Page_Capacity") // SECTOR_SIZE_BYTES
    sectors_per_resource = (
        _config_value(root, "Block_No_Per_Plane")
        * _config_value(root, "Page_No_Per_Block")
        * sectors_per_page
    )
    overprovisioning_ratio = float(root.findtext(".//Overprovisioning_Ratio"))
    return resources, sectors_per_resource, resource_count, overprovisioning_ratio


def _write_workload(
    tier_dir: Path,
    streams: list[StreamTrace],
    config_path: Path,
) -> tuple[Path, list[dict[str, object]]]:
    if len(streams) > MAX_MQSIM_FLOWS:
        raise ValueError(f"MQSim supports at most {MAX_MQSIM_FLOWS} active flows per tier")

    resources, sectors_per_resource, resource_count, overprovisioning_ratio = _config_resources(
        config_path
    )
    sectors_per_flow = (
        int(sectors_per_resource * (1 - overprovisioning_ratio) / len(streams))
        * resource_count
    )
    root = ET.Element("MQSim_IO_Scenarios")
    scenario = ET.SubElement(root, "IO_Scenario")
    stream_manifests = []

    for flow_id, stream in enumerate(streams):
        required_sectors = stream.next_slot * stream.block_size_bytes // SECTOR_SIZE_BYTES
        if required_sectors > sectors_per_flow:
            raise ValueError(
                f"{stream.tier} stream {stream.source_stream_id} requires "
                f"{required_sectors} sectors, MQSim config provides {sectors_per_flow}"
            )

        flow = ET.SubElement(scenario, "IO_Flow_Parameter_Set_Trace_Based")
        values = {
            "Priority_Class": "HIGH",
            "Device_Level_Data_Caching_Mode": "TURNED_OFF",
            **resources,
            "Initial_Occupancy_Percentage": "0",
            "File_Path": str(stream.path.resolve()),
            "Percentage_To_Be_Executed": "100",
            "Relay_Count": "1",
            "Time_Unit": "NANOSECOND",
        }
        for name, value in values.items():
            ET.SubElement(flow, name).text = value

        manifest = stream.manifest(flow_id)
        manifest["mqsim_capacity_bytes"] = sectors_per_flow * SECTOR_SIZE_BYTES
        stream_manifests.append(manifest)

    ET.indent(root, space="  ")
    workload_path = tier_dir / "workload.xml"
    ET.ElementTree(root).write(workload_path, encoding="us-ascii", xml_declaration=True)
    return workload_path, stream_manifests


def convert_trace(
    trace_path: Path,
    metrics_path: Path,
    output_dir: Path,
    configs: dict[str, Path],
    event_limit: int | None = None,
) -> dict[str, object]:
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    timestamp_unit = metrics["time"]["unit"]
    timestamp_factor = TIMESTAMP_TO_NANOSECONDS[timestamp_unit]
    block_size_bytes = int(metrics["configuration"]["block_size_bytes"])
    output_dir.mkdir(parents=True, exist_ok=True)

    writers: dict[tuple[str, int], StreamTrace] = {}
    origins: dict[str, int] = {}
    event_count = 0

    try:
        with trace_path.open(newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                if event_limit is not None and event_count >= event_limit:
                    break

                tier = row["storage_tier"].lower()
                timestamp = int(row["timestamp"])
                origins.setdefault(tier, timestamp)
                stream_id = int(row["stream_id"])
                key = (tier, stream_id)

                if key not in writers:
                    tier_dir = output_dir / tier
                    tier_dir.mkdir(parents=True, exist_ok=True)
                    writers[key] = StreamTrace.open(
                        tier,
                        stream_id,
                        tier_dir / f"stream-{stream_id}.trace",
                        block_size_bytes,
                    )

                timestamp_ns = (timestamp - origins[tier]) * timestamp_factor
                writers[key].emit(row, timestamp_ns)
                event_count += 1
    finally:
        for writer in writers.values():
            writer.close()

    tiers: dict[str, object] = {}
    for tier in ("slc", "tlc"):
        streams = sorted(
            (writer for key, writer in writers.items() if key[0] == tier),
            key=lambda writer: writer.source_stream_id,
        )
        if not streams:
            tiers[tier] = {"status": "no_events", "streams": []}
            continue

        workload, stream_manifests = _write_workload(
            output_dir / tier,
            streams,
            configs[tier],
        )
        tiers[tier] = {
            "status": "converted",
            "timestamp_origin": origins[tier],
            "config": str(configs[tier].resolve()),
            "workload": str(workload.resolve()),
            "streams": stream_manifests,
        }

    manifest = {
        "source_trace": str(trace_path.resolve()),
        "source_metrics": str(metrics_path.resolve()),
        "source_timestamp_unit": timestamp_unit,
        "mqsim_timestamp_unit": "NANOSECOND",
        "block_size_bytes": block_size_bytes,
        "events": event_count,
        "event_limit": event_limit,
        "tiers": tiers,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _integer_text(parent: ET.Element, name: str) -> int:
    return int(float(parent.findtext(name)))


def _read_mqsim_result(result_path: Path, streams: list[dict[str, object]]) -> dict[str, object]:
    root = ET.parse(result_path).getroot()
    flows = root.findall("Host/Host.IO_Flow")
    flow_results = []
    for stream, flow in zip(streams, flows, strict=True):
        flow_results.append(
            {
                "dwpdsim_stream_id": stream["dwpdsim_stream_id"],
                "mqsim_flow_id": stream["mqsim_flow_id"],
                "requests": _integer_text(flow, "Request_Count"),
                "reads": _integer_text(flow, "Read_Request_Count"),
                "writes": _integer_text(flow, "Write_Request_Count"),
                "trims": _integer_text(flow, "Trim_Request_Count"),
                "bytes_read": _integer_text(flow, "Bytes_Transferred_Read"),
                "bytes_written": _integer_text(flow, "Bytes_Transferred_Write"),
                "bytes_trimmed": _integer_text(flow, "Bytes_Trimmed"),
                "device_response_time_us": _integer_text(flow, "Device_Response_Time"),
                "end_to_end_delay_us": _integer_text(flow, "End_to_End_Request_Delay"),
            }
        )

    ftl = root.find("SSDDevice/SSDDevice.FTL")
    return {
        "result_xml": str(result_path.resolve()),
        "flows": flow_results,
        "ftl": {
            "flash_reads": int(ftl.attrib["Issued_Flash_Read_CMD"]),
            "flash_programs": int(ftl.attrib["Issued_Flash_Program_CMD"]),
            "flash_erases": int(ftl.attrib["Issued_Flash_Erase_CMD"]),
            "gc_executions": int(ftl.attrib["Total_GC_Executions"]),
            "trimmed_sectors": int(ftl.attrib["Trimmed_Sector_Count"]),
            "pages_invalidated_by_trim": int(ftl.attrib["Pages_Invalidated_By_Trim"]),
        },
    }


def _require_completed_flows(
    streams: list[dict[str, object]],
    flow_results: list[dict[str, object]],
    block_size_bytes: int,
) -> None:
    for stream, flow in zip(streams, flow_results, strict=True):
        operations = stream["operations"]
        expected = (
            stream["events"],
            operations["READ"] * block_size_bytes,
            operations["WRITE"] * block_size_bytes,
            operations["TRIM"] * block_size_bytes,
        )
        actual = (
            flow["requests"],
            flow["bytes_read"],
            flow["bytes_written"],
            flow["bytes_trimmed"],
        )
        if actual != expected:
            raise RuntimeError(
                f"MQSim did not complete every request for DWPDSim stream "
                f"{stream['dwpdsim_stream_id']}"
            )


def run_mqsim(
    mqsim_binary: Path,
    output_dir: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    results: dict[str, object] = {}
    tiers = manifest["tiers"]
    for tier in ("slc", "tlc"):
        tier_manifest = tiers[tier]
        if tier_manifest["status"] == "no_events":
            results[tier] = {"status": "no_events"}
            continue

        workload_path = Path(tier_manifest["workload"])
        subprocess.run(
            [
                str(mqsim_binary.resolve()),
                "-i",
                str(Path(tier_manifest["config"]).resolve()),
                "-w",
                str(workload_path.resolve()),
            ],
            stdin=subprocess.DEVNULL,
            check=True,
        )
        result_path = workload_path.with_name("workload_scenario_1.xml")
        tier_result = _read_mqsim_result(result_path, tier_manifest["streams"])
        _require_completed_flows(
            tier_manifest["streams"],
            tier_result["flows"],
            manifest["block_size_bytes"],
        )
        results[tier] = {
            "status": "completed",
            **tier_result,
        }

    summary = {"conversion": manifest, "mqsim": results}
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="DWPDSim generic CSV trace")
    parser.add_argument("metrics", type=Path, help="DWPDSim metrics JSON")
    parser.add_argument("--mqsim-binary", type=Path, required=True)
    parser.add_argument("--slc-config", type=Path, required=True)
    parser.add_argument("--tlc-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--event-limit", type=int)
    args = parser.parse_args()

    manifest = convert_trace(
        args.trace,
        args.metrics,
        args.output,
        {"slc": args.slc_config, "tlc": args.tlc_config},
        args.event_limit,
    )
    summary = run_mqsim(args.mqsim_binary, args.output, manifest)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
