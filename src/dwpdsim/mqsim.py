"""Convert a canonical DWPDSim trace into one dependency-aware MQSim scenario."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SECTOR_SIZE_BYTES = 512
MAX_NVME_SECTORS = 65_535
MAX_MQSIM_FLOWS = 8
TRACE_FORMAT = "DWPDSIM_DEPENDENCY_V1"
OPERATION_CODES = {"WRITE": 0, "READ": 1, "TRIM": 2}
TRACE_REASONS = {
    "STORAGE_HIT",
    "MEMORY_DUMP",
    "CAPACITY_EVICTION",
    "IDLE_EVICTION",
    "ACCESS_MIGRATION",
    "BACKGROUND_MIGRATION",
}
CANONICAL_TRACE_FIELDS = [
    "sequence",
    "timestamp_ns",
    "request_id",
    "access_sequence",
    "operation",
    "storage_tier",
    "stream_id",
    "offset_bytes",
    "length_bytes",
    "node_id",
    "hash_id",
    "reason",
    "move_id",
    "depends_on_sequence",
]
RELOCATION_REASONS = {
    "ACCESS_MIGRATION",
    "BACKGROUND_MIGRATION",
    "CAPACITY_EVICTION",
}


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    sequence: int
    timestamp_ns: int
    operation: str
    tier: str
    stream_id: int
    start_lba: int
    sector_count: int
    node_id: int
    reason: str
    move_id: int | None
    predecessor_sequence: int | None


@dataclass(frozen=True, slots=True)
class Command:
    request_id: int
    source_sequence: int
    chunk_index: int
    chunk_count: int
    timestamp_ns: int
    flow_id: int
    tier: str
    stream_id: int
    operation: str
    start_lba: int
    sector_count: int
    node_id: int
    reason: str
    move_id: int | None
    predecessor_ids: tuple[int, ...]


@dataclass(slots=True)
class Flow:
    flow_id: int
    tier: str
    stream_id: int
    path: Path
    commands: list[Command] = field(default_factory=list)

    def manifest(self) -> dict[str, object]:
        operations = {name: 0 for name in OPERATION_CODES}
        bytes_by_operation = {name: 0 for name in OPERATION_CODES}
        for command in self.commands:
            operations[command.operation] += 1
            bytes_by_operation[command.operation] += (
                command.sector_count * SECTOR_SIZE_BYTES
            )
        return {
            "flow_id": self.flow_id,
            "pool_id": self.tier,
            "dwpdsim_tier_local_stream_id": self.stream_id,
            "trace": str(self.path.resolve()),
            "commands": len(self.commands),
            "operations": operations,
            "bytes": bytes_by_operation,
        }


@dataclass(frozen=True, slots=True)
class PoolConfiguration:
    pool_id: str
    channel_ids: tuple[int, ...]
    logical_capacity_bytes: int
    media_profile_id: str
    flash_technology: str


@dataclass(slots=True)
class _AddressState:
    last_mutation: int | None = None
    readers: set[int] = field(default_factory=set)


class _AddressDependencyTracker:
    def __init__(self) -> None:
        self._states: dict[tuple[str, int, int, int], _AddressState] = {}

    def update(
        self,
        tier: str,
        stream_id: int,
        start: int,
        count: int,
        operation: str,
        request_id: int,
    ) -> set[int]:
        state = self._states.setdefault(
            (tier, stream_id, start, count),
            _AddressState(),
        )
        dependencies: set[int] = set()
        if state.last_mutation is not None:
            dependencies.add(state.last_mutation)
        if operation == "READ":
            state.readers.add(request_id)
        else:
            dependencies.update(state.readers)
            state.readers.clear()
            state.last_mutation = request_id
        return dependencies


def _optional_int(value: str) -> int | None:
    return None if value == "" else int(value)


def _nonnegative_int(
    value: str,
    field_name: str,
    source: str = "canonical trace",
) -> int:
    result = int(value)
    if result < 0:
        raise ValueError(f"{source} {field_name} must be nonnegative")
    return result


def _validate_relocations(events: list[CanonicalEvent]) -> None:
    moved: dict[tuple[int, int], list[CanonicalEvent]] = {}
    for event in events:
        if event.move_id is None:
            if event.predecessor_sequence is not None:
                raise ValueError(
                    "non-relocation canonical events cannot declare depends_on_sequence"
                )
            continue
        moved.setdefault((event.move_id, event.node_id), []).append(event)

    storage_hit_reads_by_move: dict[int, int] = {}
    reason_by_move: dict[int, str] = {}
    for (move_id, node_id), block_events in moved.items():
        if len(block_events) != 3:
            raise ValueError(
                f"relocation move {move_id} node {node_id} must contain READ, WRITE, TRIM"
            )
        source_read, destination_write, source_trim = block_events
        if [event.operation for event in block_events] != ["READ", "WRITE", "TRIM"]:
            raise ValueError(
                f"relocation move {move_id} node {node_id} must be ordered READ, WRITE, TRIM"
            )
        if not (
            source_read.sector_count
            == destination_write.sector_count
            == source_trim.sector_count
        ):
            raise ValueError(
                f"relocation move {move_id} node {node_id} has inconsistent lengths"
            )
        if (
            source_read.tier != "slc"
            or destination_write.tier != "tlc"
            or source_trim.tier != source_read.tier
            or source_trim.stream_id != source_read.stream_id
            or source_trim.start_lba != source_read.start_lba
        ):
            raise ValueError(
                f"relocation move {move_id} node {node_id} has inconsistent locations"
            )
        if destination_write.predecessor_sequence != source_read.sequence:
            raise ValueError(
                f"relocation move {move_id} node {node_id} WRITE must depend on READ"
            )
        if source_trim.predecessor_sequence != destination_write.sequence:
            raise ValueError(
                f"relocation move {move_id} node {node_id} TRIM must depend on WRITE"
            )
        if source_read.reason == "STORAGE_HIT":
            storage_hit_reads_by_move[move_id] = storage_hit_reads_by_move.get(move_id, 0) + 1
            expected_reason = "ACCESS_MIGRATION"
        else:
            expected_reason = source_read.reason
        if expected_reason not in RELOCATION_REASONS:
            raise ValueError(f"relocation move {move_id} has an invalid reason")
        if (
            destination_write.reason != expected_reason
            or source_trim.reason != expected_reason
        ):
            raise ValueError(f"relocation move {move_id} has inconsistent reasons")
        previous_reason = reason_by_move.setdefault(move_id, expected_reason)
        if previous_reason != expected_reason:
            raise ValueError(f"relocation move {move_id} has inconsistent reasons")

    for move_id, reason in reason_by_move.items():
        storage_hit_reads = storage_hit_reads_by_move.get(move_id, 0)
        if reason == "ACCESS_MIGRATION" and storage_hit_reads != 1:
            raise ValueError(
                f"access relocation move {move_id} must reuse exactly one storage-hit READ"
            )
        if reason != "ACCESS_MIGRATION" and storage_hit_reads != 0:
            raise ValueError(f"relocation move {move_id} has an invalid storage-hit READ")


def _read_events(
    trace_path: Path,
    stream_counts: dict[str, int],
    capacities: dict[str, int],
    event_limit: int | None,
) -> tuple[list[CanonicalEvent], int]:
    if event_limit is not None and event_limit < 0:
        raise ValueError("event_limit must be nonnegative")
    events: list[CanonicalEvent] = []
    seen_sequences: set[int] = set()
    last_sequence = -1
    last_timestamp = -1
    actual_event_count = 0
    with trace_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != CANONICAL_TRACE_FIELDS:
            raise ValueError("canonical trace header does not match schema version 4")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("canonical trace row does not match schema version 4")
            sequence = _nonnegative_int(row["sequence"], "sequence")
            timestamp_ns = _nonnegative_int(row["timestamp_ns"], "timestamp_ns")
            operation = row["operation"]
            storage_tier = row["storage_tier"]
            tier = storage_tier.lower()
            stream_id = _nonnegative_int(row["stream_id"], "stream_id")
            offset_bytes = _nonnegative_int(row["offset_bytes"], "offset_bytes")
            length_bytes = _nonnegative_int(row["length_bytes"], "length_bytes")
            node_id = _nonnegative_int(row["node_id"], "node_id")
            _nonnegative_int(row["hash_id"], "hash_id")
            if row["request_id"]:
                _nonnegative_int(row["request_id"], "request_id")
            if row["access_sequence"]:
                _nonnegative_int(row["access_sequence"], "access_sequence")
            move_id = _optional_int(row["move_id"])
            if move_id is not None and move_id < 0:
                raise ValueError("canonical trace move_id must be nonnegative")
            predecessor = _optional_int(row["depends_on_sequence"])
            if predecessor is not None and predecessor < 0:
                raise ValueError(
                    "canonical trace depends_on_sequence must be nonnegative"
                )

            if sequence <= last_sequence or sequence in seen_sequences:
                raise ValueError("canonical trace sequence must be strictly increasing")
            if timestamp_ns < last_timestamp:
                raise ValueError("canonical trace timestamp_ns must be nondecreasing")
            if operation not in OPERATION_CODES:
                raise ValueError(f"unsupported operation: {operation}")
            if storage_tier not in {"SLC", "TLC"}:
                raise ValueError(f"unsupported storage tier: {storage_tier}")
            if row["reason"] not in TRACE_REASONS:
                raise ValueError(f"unsupported trace reason: {row['reason']}")
            if stream_id < 0 or stream_id >= stream_counts[tier]:
                raise ValueError(f"stream {stream_id} is outside the {tier} tier")
            if offset_bytes % SECTOR_SIZE_BYTES or length_bytes % SECTOR_SIZE_BYTES:
                raise ValueError("offset_bytes and length_bytes must be 512-byte aligned")
            if length_bytes == 0:
                raise ValueError("trace length_bytes must be positive")
            if offset_bytes + length_bytes > capacities[tier]:
                raise ValueError(f"{tier} trace address exceeds configured logical capacity")
            if predecessor is not None and predecessor not in seen_sequences:
                raise ValueError("depends_on_sequence must identify an earlier trace event")

            if event_limit is None or len(events) < event_limit:
                events.append(
                    CanonicalEvent(
                        sequence=sequence,
                        timestamp_ns=timestamp_ns,
                        operation=operation,
                        tier=tier,
                        stream_id=stream_id,
                        start_lba=offset_bytes // SECTOR_SIZE_BYTES,
                        sector_count=length_bytes // SECTOR_SIZE_BYTES,
                        node_id=node_id,
                        reason=row["reason"],
                        move_id=move_id,
                        predecessor_sequence=predecessor,
                    )
                )
            actual_event_count += 1
            seen_sequences.add(sequence)
            last_sequence = sequence
            last_timestamp = timestamp_ns
    if event_limit is None:
        _validate_relocations(events)
    return events, actual_event_count


def _flow_id(tier: str, stream_id: int, slc_stream_count: int) -> int:
    return stream_id if tier == "slc" else slc_stream_count + stream_id


def _required_text(parent: ET.Element, name: str) -> str:
    value = parent.findtext(name)
    if value is None or value == "":
        raise ValueError(f"SSD configuration is missing {name}")
    return value


def _mqsim_configuration_hash(content: bytes) -> int:
    value = 14_695_981_039_346_656_037
    for byte in content:
        value ^= byte
        value = (value * 1_099_511_628_211) & ((1 << 64) - 1)
    return value


def _read_ssd_configuration(
    path: Path,
    capacities: dict[str, int],
) -> dict[str, object]:
    root = ET.parse(path).getroot()
    device = root.find(".//Device_Parameter_Set")
    if device is None:
        raise ValueError("SSD configuration is missing Device_Parameter_Set")
    required_device_values = {
        "Enabled_Preconditioning": "false",
        "Memory_Type": "FLASH",
        "HostInterface_Type": "NVME",
        "Address_Mapping": "PAGE_LEVEL",
    }
    for field_name, expected_value in required_device_values.items():
        if _required_text(device, field_name) != expected_value:
            raise ValueError(
                f"SSD configuration {field_name} must be {expected_value}"
            )
    start_ns = _nonnegative_int(
        _required_text(device, "Measurement_Start_Time_Ns"),
        "Measurement_Start_Time_Ns",
        "SSD configuration",
    )
    end_ns = _nonnegative_int(
        _required_text(device, "Measurement_End_Time_Ns"),
        "Measurement_End_Time_Ns",
        "SSD configuration",
    )
    if start_ns >= end_ns:
        raise ValueError("SSD measurement window must satisfy start_ns < end_ns")

    profiles = {
        _required_text(profile, "Media_Profile_ID"): _required_text(
            profile,
            "Flash_Technology",
        )
        for profile in root.findall(".//Flash_Media_Profile")
    }
    pools: dict[str, PoolConfiguration] = {}
    used_channels: set[int] = set()
    for element in root.findall(".//Flash_Pool_Parameter_Set"):
        pool_id = _required_text(element, "Pool_ID")
        if pool_id not in {"slc", "tlc"}:
            raise ValueError(
                "SSD configuration must contain exactly the slc and tlc pools"
            )
        if pool_id in pools:
            raise ValueError(f"SSD configuration repeats pool {pool_id}")
        channel_text = _required_text(element, "Channel_IDs")
        channel_ids = tuple(int(value) for value in channel_text.split(","))
        if not channel_ids or any(channel < 0 for channel in channel_ids):
            raise ValueError(f"SSD pool {pool_id} has invalid channels")
        if len(set(channel_ids)) != len(channel_ids):
            raise ValueError(f"SSD pool {pool_id} repeats a channel")
        overlap = used_channels.intersection(channel_ids)
        if overlap:
            raise ValueError("SSD pool channel sets must be disjoint")
        used_channels.update(channel_ids)
        capacity_sectors = _nonnegative_int(
            _required_text(element, "Logical_Capacity_In_Sectors"),
            "Logical_Capacity_In_Sectors",
            "SSD configuration",
        )
        media_profile_id = _required_text(element, "Media_Profile_ID")
        if media_profile_id not in profiles:
            raise ValueError(
                f"SSD pool {pool_id} references unknown media profile {media_profile_id}"
            )
        flash_technology = profiles[media_profile_id]
        if flash_technology.lower() != pool_id:
            raise ValueError(
                f"SSD pool {pool_id} media profile must use {pool_id.upper()} technology"
            )
        pools[pool_id] = PoolConfiguration(
            pool_id=pool_id,
            channel_ids=channel_ids,
            logical_capacity_bytes=capacity_sectors * SECTOR_SIZE_BYTES,
            media_profile_id=media_profile_id,
            flash_technology=flash_technology,
        )

    if set(pools) != {"slc", "tlc"}:
        raise ValueError("SSD configuration must contain exactly the slc and tlc pools")
    for pool_id, expected_capacity in capacities.items():
        if pools[pool_id].logical_capacity_bytes != expected_capacity:
            raise ValueError(
                f"SSD pool {pool_id} logical capacity differs from DWPDSim metrics"
            )

    content = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(content).hexdigest(),
        "mqsim_configuration_hash": _mqsim_configuration_hash(content),
        "mqsim_configuration_hash_algorithm": "fnv1a64-raw-xml",
        "measurement_window": {
            "start_ns": start_ns,
            "end_ns": end_ns,
        },
        "pools": {
            pool_id: {
                "pool_id": pool.pool_id,
                "channel_ids": list(pool.channel_ids),
                "logical_capacity_bytes": pool.logical_capacity_bytes,
                "media_profile_id": pool.media_profile_id,
                "flash_technology": pool.flash_technology,
            }
            for pool_id, pool in pools.items()
        },
    }


def _commands(
    events: list[CanonicalEvent],
    slc_stream_count: int,
) -> list[Command]:
    result: list[Command] = []
    final_command_by_sequence: dict[int, int] = {}
    hazards = _AddressDependencyTracker()
    next_request_id = 0

    for event in events:
        chunk_count = (event.sector_count + MAX_NVME_SECTORS - 1) // MAX_NVME_SECTORS
        previous_chunk_id: int | None = None
        remaining = event.sector_count
        start_lba = event.start_lba
        for chunk_index in range(chunk_count):
            sector_count = min(remaining, MAX_NVME_SECTORS)
            request_id = next_request_id
            next_request_id += 1
            dependencies = hazards.update(
                event.tier,
                event.stream_id,
                start_lba,
                sector_count,
                event.operation,
                request_id,
            )
            if chunk_index == 0 and event.predecessor_sequence is not None:
                dependencies.add(final_command_by_sequence[event.predecessor_sequence])
            if previous_chunk_id is not None:
                dependencies.add(previous_chunk_id)
            dependencies.discard(request_id)

            result.append(
                Command(
                    request_id=request_id,
                    source_sequence=event.sequence,
                    chunk_index=chunk_index,
                    chunk_count=chunk_count,
                    timestamp_ns=event.timestamp_ns,
                    flow_id=_flow_id(event.tier, event.stream_id, slc_stream_count),
                    tier=event.tier,
                    stream_id=event.stream_id,
                    operation=event.operation,
                    start_lba=start_lba,
                    sector_count=sector_count,
                    node_id=event.node_id,
                    reason=event.reason,
                    move_id=event.move_id,
                    predecessor_ids=tuple(sorted(dependencies)),
                )
            )
            previous_chunk_id = request_id
            start_lba += sector_count
            remaining -= sector_count
        final_command_by_sequence[event.sequence] = previous_chunk_id
    return result


def _write_command_trace(flow: Flow) -> None:
    with flow.path.open("w", encoding="utf-8") as output:
        for command in flow.commands:
            dependencies = (
                ",".join(str(value) for value in command.predecessor_ids)
                if command.predecessor_ids
                else "-1"
            )
            output.write(
                f"{command.timestamp_ns} 0 {command.start_lba} "
                f"{command.sector_count} {OPERATION_CODES[command.operation]} "
                f"{command.request_id} {dependencies}\n"
            )


def _write_command_manifest(path: Path, commands: list[Command]) -> None:
    fields = [
        "command_request_id",
        "source_sequence",
        "chunk_index",
        "chunk_count",
        "flow_id",
        "pool_id",
        "tier_local_stream_id",
        "operation",
        "start_lba",
        "sector_count",
        "node_id",
        "reason",
        "move_id",
        "depends_on_request_ids",
    ]
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for command in commands:
            writer.writerow(
                {
                    "command_request_id": command.request_id,
                    "source_sequence": command.source_sequence,
                    "chunk_index": command.chunk_index,
                    "chunk_count": command.chunk_count,
                    "flow_id": command.flow_id,
                    "pool_id": command.tier,
                    "tier_local_stream_id": command.stream_id,
                    "operation": command.operation,
                    "start_lba": command.start_lba,
                    "sector_count": command.sector_count,
                    "node_id": command.node_id,
                    "reason": command.reason,
                    "move_id": "" if command.move_id is None else command.move_id,
                    "depends_on_request_ids": ",".join(
                        str(value) for value in command.predecessor_ids
                    )
                    or "-1",
                }
            )


def _write_workload(output_dir: Path, flows: list[Flow]) -> Path:
    root = ET.Element("MQSim_IO_Scenarios")
    scenario = ET.SubElement(root, "IO_Scenario")
    for flow in flows:
        element = ET.SubElement(scenario, "IO_Flow_Parameter_Set_Trace_Based")
        values = {
            "Priority_Class": "HIGH",
            "Device_Level_Data_Caching_Mode": "TURNED_OFF",
            "Pool_ID": flow.tier,
            "Initial_Occupancy_Percentage": "0",
            "File_Path": str(flow.path.resolve()),
            "Trace_Format": TRACE_FORMAT,
            "Percentage_To_Be_Executed": "100",
            "Relay_Count": "1",
            "Time_Unit": "NANOSECOND",
        }
        for name, value in values.items():
            ET.SubElement(element, name).text = value
    ET.indent(root, space="  ")
    workload_path = output_dir / "workload.xml"
    ET.ElementTree(root).write(workload_path, encoding="us-ascii", xml_declaration=True)
    return workload_path


def convert_trace(
    trace_path: Path,
    metrics_path: Path,
    output_dir: Path,
    ssd_config_path: Path,
    event_limit: int | None = None,
) -> dict[str, object]:
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    if metrics["time"]["unit"] != "ns":
        raise ValueError("DWPDSim vNext metrics must use nanoseconds")
    if int(metrics["trace"]["schema_version"]) != 4:
        raise ValueError("DWPDSim canonical trace schema version must be 4")

    configuration = metrics["configuration"]
    stream_counts = {
        "slc": int(configuration["slc_stream_count"]),
        "tlc": int(configuration["tlc_stream_count"]),
    }
    capacities = {
        "slc": int(configuration["slc_capacity_bytes"]),
        "tlc": int(configuration["tlc_capacity_bytes"]),
    }
    if any(count <= 0 for count in stream_counts.values()):
        raise ValueError("DWPDSim storage stream counts must be positive")
    if any(
        capacity <= 0 or capacity % SECTOR_SIZE_BYTES != 0
        for capacity in capacities.values()
    ):
        raise ValueError("DWPDSim storage capacities must be positive 512-byte multiples")
    total_flows = stream_counts["slc"] + stream_counts["tlc"]
    if total_flows > MAX_MQSIM_FLOWS:
        raise ValueError(
            f"DWPDSim vNext requires {total_flows} MQSim flows; the V1 limit is "
            f"{MAX_MQSIM_FLOWS}"
        )
    ssd_configuration = _read_ssd_configuration(ssd_config_path, capacities)
    events, actual_event_count = _read_events(
        trace_path,
        stream_counts,
        capacities,
        event_limit,
    )
    declared_event_count = int(metrics["trace"]["events"])
    if event_limit is None and actual_event_count != declared_event_count:
        raise ValueError(
            "canonical trace row count differs from DWPDSim metrics trace.events"
        )
    commands = _commands(events, stream_counts["slc"])

    output_dir.mkdir(parents=True, exist_ok=True)
    flows: list[Flow] = []
    for tier in ("slc", "tlc"):
        for stream_id in range(stream_counts[tier]):
            flow_id = _flow_id(tier, stream_id, stream_counts["slc"])
            flows.append(
                Flow(
                    flow_id=flow_id,
                    tier=tier,
                    stream_id=stream_id,
                    path=output_dir / f"flow-{flow_id}.trace",
                )
            )
    flow_by_id = {flow.flow_id: flow for flow in flows}
    for command in commands:
        flow_by_id[command.flow_id].commands.append(command)
    for flow in flows:
        _write_command_trace(flow)

    command_manifest_path = output_dir / "commands.csv"
    _write_command_manifest(command_manifest_path, commands)
    workload_path = _write_workload(output_dir, flows)
    flow_manifests = [flow.manifest() for flow in flows]
    pool_manifests: dict[str, dict[str, object]] = {}
    for tier in ("slc", "tlc"):
        tier_flows = [flow for flow in flow_manifests if flow["pool_id"] == tier]
        pool_manifests[tier] = {
            **ssd_configuration["pools"][tier],
            "stream_count": stream_counts[tier],
            "commands": sum(int(flow["commands"]) for flow in tier_flows),
            "operations": {
                operation: sum(
                    int(flow["operations"][operation]) for flow in tier_flows
                )
                for operation in OPERATION_CODES
            },
            "bytes": {
                operation: sum(int(flow["bytes"][operation]) for flow in tier_flows)
                for operation in OPERATION_CODES
            },
            "dwpdsim_dump_host_write_bytes": int(
                metrics.get("storage", {}).get(tier, {}).get("host_write_bytes", 0)
            ),
        }
    manifest = {
        "trace_format": TRACE_FORMAT,
        "dependency_field": "depends_on_request_ids",
        "source_trace": str(trace_path.resolve()),
        "source_metrics": str(metrics_path.resolve()),
        "ssd_config": {
            "path": ssd_configuration["path"],
            "sha256": ssd_configuration["sha256"],
            "mqsim_configuration_hash": ssd_configuration[
                "mqsim_configuration_hash"
            ],
            "mqsim_configuration_hash_algorithm": ssd_configuration[
                "mqsim_configuration_hash_algorithm"
            ],
        },
        "workload": str(workload_path.resolve()),
        "command_manifest": str(command_manifest_path.resolve()),
        "time": {
            "unit": "ns",
            "preserved_absolute_timestamps": True,
            "global_time_origin_ns": 0,
            "measurement_start_ns": ssd_configuration["measurement_window"][
                "start_ns"
            ],
            "measurement_end_ns": ssd_configuration["measurement_window"]["end_ns"],
        },
        "source_semantic_events": len(events),
        "source_trace_events_declared": declared_event_count,
        "source_trace_events_actual": actual_event_count,
        "commands": len(commands),
        "event_limit": event_limit,
        "flow_mapping": "SLC streams followed by TLC streams",
        "max_flows": MAX_MQSIM_FLOWS,
        "flows": flow_manifests,
        "pools": pool_manifests,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _integer_text(parent: ET.Element, name: str) -> int:
    value = parent.findtext(name)
    if value is None:
        raise RuntimeError(f"MQSim result is missing {name}")
    try:
        return int(value)
    except ValueError as error:
        raise RuntimeError(f"MQSim result {name} is not an integer") from error


def _required_attribute(parent: ET.Element, name: str) -> str:
    value = parent.get(name)
    if value is None:
        raise RuntimeError(f"MQSim result is missing {name}")
    return value


def _integer_attribute(parent: ET.Element, name: str) -> int:
    value = _required_attribute(parent, name)
    try:
        return int(value)
    except ValueError as error:
        raise RuntimeError(f"MQSim result {name} is not an integer") from error


def _read_host_results(
    root: ET.Element,
    flows: list[dict[str, object]],
) -> list[dict[str, object]]:
    expected_by_id = {int(flow["flow_id"]): flow for flow in flows}
    actual_by_id: dict[int, dict[str, object]] = {}
    for element in root.findall("Host/Host.IO_Flow"):
        flow_id = _integer_text(element, "Flow_ID")
        if flow_id in actual_by_id:
            raise RuntimeError(f"MQSim result repeats flow {flow_id}")
        if element.findtext("Time_Unit") != "nanosecond":
            raise RuntimeError(f"MQSim flow {flow_id} has the wrong time unit")
        actual_by_id[flow_id] = {
            "flow_id": flow_id,
            "pool_id": element.findtext("Pool_ID"),
            "generated": _integer_text(element, "Generated_Request_Count"),
            "completed": _integer_text(element, "Completed_Request_Count"),
            "reads": _integer_text(element, "Read_Request_Count"),
            "writes": _integer_text(element, "Write_Request_Count"),
            "trims": _integer_text(element, "Trim_Request_Count"),
            "bytes_read": _integer_text(element, "Bytes_Transferred_Read"),
            "bytes_written": _integer_text(element, "Bytes_Transferred_Write"),
            "bytes_trimmed_requested": _integer_text(
                element,
                "Bytes_Trimmed_Requested",
            ),
            "measurement_host_write_bytes": _integer_text(
                element,
                "Measurement_Host_Write_Bytes",
            ),
            "dependency_wait_time_total_ns": _integer_text(
                element,
                "Dependency_Wait_Time_Total",
            ),
            "dependency_wait_time_max_ns": _integer_text(
                element,
                "Dependency_Wait_Time_Max",
            ),
        }
    if set(actual_by_id) != set(expected_by_id):
        raise RuntimeError("MQSim result flow IDs differ from the converter manifest")

    for flow_id, expected in expected_by_id.items():
        actual = actual_by_id[flow_id]
        if actual["pool_id"] != expected["pool_id"]:
            raise RuntimeError(f"MQSim flow {flow_id} has the wrong pool")
        operations = expected["operations"]
        byte_counts = expected["bytes"]
        expected_values = (
            expected["commands"],
            expected["commands"],
            operations["READ"],
            operations["WRITE"],
            operations["TRIM"],
            byte_counts["READ"],
            byte_counts["WRITE"],
            byte_counts["TRIM"],
        )
        actual_values = (
            actual["generated"],
            actual["completed"],
            actual["reads"],
            actual["writes"],
            actual["trims"],
            actual["bytes_read"],
            actual["bytes_written"],
            actual["bytes_trimmed_requested"],
        )
        if actual_values != expected_values:
            raise RuntimeError(f"MQSim flow {flow_id} did not match its manifest")
    return [actual_by_id[int(flow["flow_id"])] for flow in flows]


FTL_INTEGER_ATTRIBUTES = (
    "Received_Trim_Command_Count",
    "Requested_Trim_Sector_Count",
    "Effective_Trimmed_Sector_Count",
    "Pages_Invalidated_By_Trim",
    "GC_Execution_Count",
    "GC_Page_Read_Count",
    "GC_Page_Program_Count",
)

POOL_INTEGER_ATTRIBUTES = (
    "Host_Read_Bytes",
    "Host_Write_Bytes",
    "Requested_Trim_Bytes",
    "Effective_Trimmed_Bytes",
    "Received_Trim_Command_Count",
    "Requested_Trim_Sector_Count",
    "Effective_Trimmed_Sector_Count",
    "Pages_Invalidated_By_Trim",
    "GC_Execution_Count",
    "GC_Page_Read_Count",
    "GC_Page_Program_Count",
    "Flash_Read_Command_Count",
    "Flash_Program_Command_Count",
    "Flash_Erase_Command_Count",
    "Total_Block_Erase_Count",
    "Max_Block_Erase_Count",
    "Measurement_Total_Block_Erase_Count",
    "Measurement_Max_Block_Erase_Count",
    "Measurement_Host_Write_Bytes",
    "Measurement_Flash_Programmed_Bytes",
    "Logical_Capacity_Bytes",
    "Physical_Capacity_Bytes",
    "PE_Cycle_Limit",
)

CHANNEL_INTEGER_ATTRIBUTES = (
    "Host_Read_Bytes",
    "Host_Write_Bytes",
    "Requested_Trim_Bytes",
    "Effective_Trimmed_Bytes",
    "Requested_Trim_Sector_Count",
    "Effective_Trimmed_Sector_Count",
    "Flash_Read_Command_Count",
    "Flash_Program_Command_Count",
    "Flash_Erase_Command_Count",
    "Total_Block_Erase_Count",
    "Max_Block_Erase_Count",
    "Measurement_Total_Block_Erase_Count",
    "Measurement_Max_Block_Erase_Count",
    "Measurement_Flash_Programmed_Bytes",
    "Logical_Capacity_Bytes",
    "Physical_Capacity_Bytes",
    "PE_Cycle_Limit",
)


def _ratio(numerator: int, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _read_pool_results(
    root: ET.Element,
    manifest: dict[str, object],
    measurement_days: float,
) -> list[dict[str, object]]:
    expected_pools = manifest["pools"]
    actual_by_id: dict[str, dict[str, object]] = {}
    for element in root.findall("SSDDevice/SSDDevice.Pool"):
        pool_id = _required_attribute(element, "ID")
        if pool_id in actual_by_id:
            raise RuntimeError(f"MQSim result repeats pool {pool_id}")
        statistics = {
            name: _integer_attribute(element, name) for name in POOL_INTEGER_ATTRIBUTES
        }
        actual_by_id[pool_id] = {
            "pool_id": pool_id,
            "media_profile_id": _required_attribute(element, "Media_Profile_ID"),
            "channel_ids": [
                int(value)
                for value in _required_attribute(element, "Channel_IDs").split(",")
            ],
            "statistics": statistics,
        }
    if set(actual_by_id) != set(expected_pools):
        raise RuntimeError("MQSim result pool IDs differ from the converter manifest")

    results: list[dict[str, object]] = []
    for pool_id in ("slc", "tlc"):
        expected = expected_pools[pool_id]
        actual = actual_by_id[pool_id]
        statistics = actual["statistics"]
        if actual["media_profile_id"] != expected["media_profile_id"]:
            raise RuntimeError(f"MQSim pool {pool_id} has the wrong media profile")
        if actual["channel_ids"] != expected["channel_ids"]:
            raise RuntimeError(f"MQSim pool {pool_id} has the wrong channel mapping")
        if statistics["Logical_Capacity_Bytes"] != expected["logical_capacity_bytes"]:
            raise RuntimeError(f"MQSim pool {pool_id} has the wrong logical capacity")
        byte_counts = expected["bytes"]
        if (
            statistics["Host_Read_Bytes"] != byte_counts["READ"]
            or statistics["Host_Write_Bytes"] != byte_counts["WRITE"]
            or statistics["Requested_Trim_Bytes"] != byte_counts["TRIM"]
            or statistics["Received_Trim_Command_Count"]
            != expected["operations"]["TRIM"]
            or statistics["Requested_Trim_Sector_Count"]
            != byte_counts["TRIM"] // SECTOR_SIZE_BYTES
        ):
            raise RuntimeError(f"MQSim pool {pool_id} host I/O did not match its manifest")

        logical_capacity = statistics["Logical_Capacity_Bytes"]
        measurement_host_writes = statistics["Measurement_Host_Write_Bytes"]
        measurement_programs = statistics["Measurement_Flash_Programmed_Bytes"]
        derived = {
            "measurement_days": measurement_days,
            "host_dwpd": _ratio(
                measurement_host_writes,
                logical_capacity * measurement_days,
            ),
            "nand_dwpd": _ratio(
                measurement_programs,
                logical_capacity * measurement_days,
            ),
            "write_amplification": _ratio(
                measurement_programs,
                measurement_host_writes,
            ),
            "max_block_pe_per_day": _ratio(
                statistics["Measurement_Max_Block_Erase_Count"],
                measurement_days,
            ),
        }
        results.append(
            {
                **actual,
                "dwpdsim_dump_host_write_bytes": expected[
                    "dwpdsim_dump_host_write_bytes"
                ],
                "mqsim_host_write_bytes_all_commands": statistics[
                    "Host_Write_Bytes"
                ],
                "derived": derived,
            }
        )
    return results


def _read_channel_results(
    root: ET.Element,
    manifest: dict[str, object],
) -> list[dict[str, object]]:
    expected_channels = {
        int(channel_id): (pool_id, pool["media_profile_id"])
        for pool_id, pool in manifest["pools"].items()
        for channel_id in pool["channel_ids"]
    }
    actual_by_id: dict[int, dict[str, object]] = {}
    for element in root.findall("SSDDevice/SSDDevice.Channel"):
        channel_id = _integer_attribute(element, "ID")
        if channel_id in actual_by_id:
            raise RuntimeError(f"MQSim result repeats channel {channel_id}")
        actual_by_id[channel_id] = {
            "channel_id": channel_id,
            "pool_id": _required_attribute(element, "Pool_ID"),
            "media_profile_id": _required_attribute(element, "Media_Profile_ID"),
            "statistics": {
                name: _integer_attribute(element, name)
                for name in CHANNEL_INTEGER_ATTRIBUTES
            },
        }
    if set(actual_by_id) != set(expected_channels):
        raise RuntimeError("MQSim result channel IDs differ from the SSD configuration")
    for channel_id, (pool_id, profile_id) in expected_channels.items():
        actual = actual_by_id[channel_id]
        if actual["pool_id"] != pool_id or actual["media_profile_id"] != profile_id:
            raise RuntimeError(f"MQSim channel {channel_id} has the wrong pool mapping")
    return [actual_by_id[channel_id] for channel_id in sorted(actual_by_id)]


def read_mqsim_results(
    result_path: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    root = ET.parse(result_path).getroot()
    if root.tag != "MQSim_Results":
        raise RuntimeError("MQSim result root must be MQSim_Results")
    configurations = root.findall("SSDDevice/SSDDevice.Configuration")
    if len(configurations) != 1:
        raise RuntimeError("MQSim result must contain one SSD configuration record")
    configuration = configurations[0]
    expected_time = manifest["time"]
    result_configuration = {
        "simulator_version": _required_attribute(configuration, "Simulator_Version"),
        "statistics_abi_version": _integer_attribute(
            configuration,
            "Statistics_ABI_Version",
        ),
        "configuration_hash": _integer_attribute(configuration, "Configuration_Hash"),
        "configuration_hash_algorithm": _required_attribute(
            configuration,
            "Configuration_Hash_Algorithm",
        ),
        "time_unit": _required_attribute(configuration, "Time_Unit"),
        "measurement_start_ns": _integer_attribute(
            configuration,
            "Measurement_Start_Time_Ns",
        ),
        "measurement_end_ns": _integer_attribute(
            configuration,
            "Measurement_End_Time_Ns",
        ),
    }
    if result_configuration["statistics_abi_version"] != 1:
        raise RuntimeError("MQSim result statistics ABI version must be 1")
    if (
        result_configuration["configuration_hash_algorithm"]
        != manifest["ssd_config"]["mqsim_configuration_hash_algorithm"]
    ):
        raise RuntimeError("MQSim result has the wrong configuration hash algorithm")
    if (
        result_configuration["configuration_hash"]
        != manifest["ssd_config"]["mqsim_configuration_hash"]
    ):
        raise RuntimeError("MQSim result configuration hash differs from the manifest")
    if result_configuration["time_unit"] != "nanosecond":
        raise RuntimeError("MQSim result time unit must be nanosecond")
    if (
        result_configuration["measurement_start_ns"]
        != expected_time["measurement_start_ns"]
        or result_configuration["measurement_end_ns"]
        != expected_time["measurement_end_ns"]
    ):
        raise RuntimeError("MQSim result measurement window differs from the manifest")

    ftl_elements = root.findall("SSDDevice/SSDDevice.FTL")
    if len(ftl_elements) != 1:
        raise RuntimeError("MQSim result must contain one FTL statistics record")
    ftl = {
        name: _integer_attribute(ftl_elements[0], name)
        for name in FTL_INTEGER_ATTRIBUTES
    }
    window_ns = (
        result_configuration["measurement_end_ns"]
        - result_configuration["measurement_start_ns"]
    )
    measurement_days = window_ns / (86_400 * 1_000_000_000)
    flows = _read_host_results(root, manifest["flows"])
    pools = _read_pool_results(root, manifest, measurement_days)
    channels = _read_channel_results(root, manifest)
    for name in FTL_INTEGER_ATTRIBUTES:
        if ftl[name] != sum(pool["statistics"][name] for pool in pools):
            raise RuntimeError(f"MQSim FTL {name} differs from the pool totals")
    flow_by_pool = {
        pool_id: [flow for flow in flows if flow["pool_id"] == pool_id]
        for pool_id in ("slc", "tlc")
    }
    channel_by_pool = {
        pool_id: [channel for channel in channels if channel["pool_id"] == pool_id]
        for pool_id in ("slc", "tlc")
    }
    summed_channel_fields = (
        "Host_Read_Bytes",
        "Host_Write_Bytes",
        "Requested_Trim_Bytes",
        "Effective_Trimmed_Bytes",
        "Requested_Trim_Sector_Count",
        "Effective_Trimmed_Sector_Count",
        "Flash_Read_Command_Count",
        "Flash_Program_Command_Count",
        "Flash_Erase_Command_Count",
        "Total_Block_Erase_Count",
        "Measurement_Total_Block_Erase_Count",
        "Measurement_Flash_Programmed_Bytes",
        "Physical_Capacity_Bytes",
    )
    for pool in pools:
        pool_id = pool["pool_id"]
        statistics = pool["statistics"]
        if statistics["Measurement_Host_Write_Bytes"] != sum(
            flow["measurement_host_write_bytes"] for flow in flow_by_pool[pool_id]
        ):
            raise RuntimeError(
                f"MQSim pool {pool_id} measurement writes differ from its flows"
            )
        pool_channels = channel_by_pool[pool_id]
        for name in summed_channel_fields:
            if statistics[name] != sum(
                channel["statistics"][name] for channel in pool_channels
            ):
                raise RuntimeError(f"MQSim pool {pool_id} {name} differs from its channels")
        for name in ("Max_Block_Erase_Count", "Measurement_Max_Block_Erase_Count"):
            if statistics[name] != max(
                channel["statistics"][name] for channel in pool_channels
            ):
                raise RuntimeError(f"MQSim pool {pool_id} {name} differs from its channels")
        if any(
            channel["statistics"]["PE_Cycle_Limit"] != statistics["PE_Cycle_Limit"]
            for channel in pool_channels
        ):
            raise RuntimeError(f"MQSim pool {pool_id} PE limit differs from its channels")
    return {
        "result_xml": str(result_path.resolve()),
        "configuration": result_configuration,
        "ftl": ftl,
        "flows": flows,
        "pools": pools,
        "channels": channels,
    }


def run_mqsim(
    mqsim_binary: Path,
    output_dir: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    workload_path = Path(manifest["workload"])
    ssd_config_path = Path(manifest["ssd_config"]["path"])
    content = ssd_config_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest["ssd_config"]["sha256"]:
        raise RuntimeError("SSD configuration changed after trace conversion")
    subprocess.run(
        [
            str(mqsim_binary.resolve()),
            "-i",
            str(ssd_config_path.resolve()),
            "-w",
            str(workload_path.resolve()),
        ],
        stdin=subprocess.DEVNULL,
        check=True,
    )
    result_path = workload_path.with_name("workload_scenario_1.xml")
    summary = {
        "conversion": manifest,
        "mqsim": read_mqsim_results(result_path, manifest),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="DWPDSim canonical CSV trace")
    parser.add_argument("metrics", type=Path, help="DWPDSim metrics JSON")
    parser.add_argument("--mqsim-binary", type=Path, required=True)
    parser.add_argument("--ssd-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--event-limit", type=int)
    args = parser.parse_args()

    manifest = convert_trace(
        args.trace,
        args.metrics,
        args.output,
        args.ssd_config,
        args.event_limit,
    )
    summary = run_mqsim(args.mqsim_binary, args.output, manifest)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
