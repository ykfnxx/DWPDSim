"""Public configuration objects for the C++ simulator."""

from dataclasses import dataclass

MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class MediumConfig:
    """Capacity and stream count for one SSD medium."""

    capacity_bytes: int
    stream_count: int


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Fixed capacities and timestamp metadata for one simulation."""

    memory_capacity_bytes: int
    slc: MediumConfig
    tlc: MediumConfig
    block_size_bytes: int = 8 * MIB
    timestamp_unit: str = "unspecified"
    progress_interval_requests: int = 0


@dataclass(frozen=True, slots=True)
class MemoryPolicyConfig:
    """Select the built-in memory policy and its two decisions."""

    kind: str = "lru"
    admit_storage_hits: bool = True
    eviction_action: str = "persist"


@dataclass(frozen=True, slots=True)
class PlacementPolicyConfig:
    """Select fixed or ratio-based SLC/TLC write placement."""

    kind: str = "fixed"
    fixed_medium: str = "tlc"
    fixed_stream_id: int = 0
    slc_write_ratio: float = 0.0


@dataclass(frozen=True, slots=True)
class StorageEvictionPolicyConfig:
    """Select storage eviction; built-in LRU demotes SLC and drops TLC segments."""

    kind: str = "lru"
