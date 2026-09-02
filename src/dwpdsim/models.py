"""Input data objects used by the convenience Python API."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Request:
    """One timestamped, ordered sequence of uint64 block hashes."""

    timestamp: int
    hash_ids: Sequence[int]
