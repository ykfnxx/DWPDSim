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
    """Memory LRU with scan, indexed, or context-aware segment selection."""

    kind: str = "baseline_lru"
    admit_storage_hits: bool = True
    groups: int = 1
    sampled_groups: int = 0
    workers: int = 1
    seed: int = 0
    profile: bool = False
    # indexed_lru and context_lru: Drop if selected segment idle time is strictly greater.
    retention_ns: int | None = None
    # context_lru only: fraction of Memory capacity covered by oldest candidates.
    alpha: float = 0.01
    # context_lru only: maximum resident blocks evicted from the selected segment tail.
    max_eviction_blocks: int | None = None
    retention_growth_seconds_per_block: float = 0.0
    eviction_gap_reference_ns: int | None = None
    eviction_base_blocks: int = 64


@dataclass(frozen=True, slots=True)
class StoragePolicyConfig:
    """Storage policy, or infinite_storage for metrics-only Memory experiments."""

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
    # RR-only exact implementations and opt-in experiment instrumentation.
    rr_victim_search: str = "indexed"
    rr_subtree_counts: bool = False
    rr_verify_victims: bool = False
    rr_profile: bool = False
    # wear_balanced only: fixed write amplification estimates, finite and >= 1.
    slc_wa: float = 1.0
    tlc_wa: float = 1.0


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
