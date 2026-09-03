"""Public configuration for DWPDSim."""

from dataclasses import dataclass, field

MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """Memory capacity."""

    capacity_bytes: int


@dataclass(frozen=True, slots=True)
class StorageTierConfig:
    """Logical capacity and tier-local stream count for one SSD pool."""

    capacity_bytes: int
    stream_count: int


@dataclass(frozen=True, slots=True)
class MemoryPolicyConfig:
    """Baseline memory LRU decisions."""

    kind: str = "baseline_lru"
    admit_storage_hits: bool = True
    eviction_action: str = "dump"


@dataclass(frozen=True, slots=True)
class StoragePolicyConfig:
    """One complete storage placement, reclaim, and maintenance policy."""

    kind: str = "baseline_fixed_lru"
    fixed_tier: str = "tlc"
    fixed_stream_id: int = 0
    slc_write_ratio: float = 0.0
    slc_host_share: float | None = None
    idle_multiplier: float = 32.0
    promotion_seconds: float = 14_400.0
    adaptation_gain: float = 2.0
    direct_gain: float = 1.0
    slc_soft_utilization: float = 0.75
    occupancy_decay: float = 8.0
    logical_fill_fraction: float = 0.98
    slc_erase_budget: float = 120.0
    tlc_erase_budget: float = 12.0
    background_period_ns: int = 900 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Capacities, policies, and logical simulation end."""

    memory: MemoryConfig
    slc: StorageTierConfig
    tlc: StorageTierConfig
    block_size_bytes: int = 8 * MIB
    memory_policy: MemoryPolicyConfig = field(default_factory=MemoryPolicyConfig)
    storage_policy: StoragePolicyConfig = field(default_factory=StoragePolicyConfig)
    simulation_end_ns: int | None = None
    progress_interval_requests: int = 0
