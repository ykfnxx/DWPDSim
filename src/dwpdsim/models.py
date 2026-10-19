"""Input data objects for DWPDSim."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Request:
    """One ordered request on the global nanosecond timeline."""

    timestamp_ns: int
    request_id: int
    affinity_id: int
    hash_ids: Sequence[int]


@dataclass(frozen=True, slots=True)
class HybridEvent:
    """An ordered observe/publish/request event in one fixed model namespace.

    Publish supplies only the final hash's component. Request consumes FULL
    blocks through restore_blocks and/or the exact LINEAR endpoint there.
    None restore_blocks means the complete path; no payload is inferred.
    """

    timestamp_ns: int
    sequence: int
    action: str
    hash_ids: Sequence[int]
    request_id: int = 0
    components: str = "both"
    restore_blocks: int | None = None
    size_bytes: int | None = None
