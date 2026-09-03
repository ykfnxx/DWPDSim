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
