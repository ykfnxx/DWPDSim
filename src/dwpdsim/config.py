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
    """Memory LRU and conditional-reuse segment selection."""

    kind: str = "baseline_lru"
    admit_storage_hits: bool = True
    groups: int = 1
    sampled_groups: int = 0
    workers: int = 1
    seed: int = 0
    profile: bool = False
    # indexed_lru, context_lru and reuse_lru: Drop if selected segment idle time is strictly greater.
    retention_ns: int | None = None
    # context_lru / reuse_lru: fraction of Memory capacity covered by oldest candidates.
    alpha: float = 0.01
    # context_lru / reuse_lru: maximum resident blocks evicted from the selected segment tail.
    max_eviction_blocks: int | None = None
    retention_growth_seconds_per_block: float = 0.0
    eviction_gap_reference_ns: int | None = None
    eviction_base_blocks: int = 64
    # reuse_lru: nine rows = previous gap (first/<=60s/>60s) x context (<=128/<=512/>512).
    reuse_edges_s: list[float] = field(default_factory=list)
    reuse_survival: list[list[float]] = field(default_factory=list)
    reuse_dump_probability: float | None = None
    reuse_priority: bool = False
    reuse_admit_probability: float | None = None
    reuse_short_horizon_s: float = 60.0
    reuse_long_horizon_s: float = 3600.0
    # Conditional prefix retention: [group][gap interval][relative depth third].
    reuse_prefix_keep: list[list[list[float]]] = field(default_factory=list)
    reuse_cost_weighted: bool = False
    reuse_recompute_weight: float = 1.0


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
    # wear_balanced: Shadow FTL and optional reuse-driven idle tuning.
    online_tuning: bool = False
    feedback_period_ns: int = 900 * 1_000_000_000
    shadow_page_bytes: int = 4096
    shadow_pages_per_block: int = 256
    shadow_overprovisioning: float = 0.07
    shadow_slc_nominal_bytes: int = 0
    shadow_tlc_nominal_bytes: int = 0
    reuse_loss_budget: float = 0.01
    min_reuse_blocks: int = 4096
    reuse_ema_scale_blocks: int = 50000
    shadow_slc_physical_blocks: int = 0
    shadow_tlc_physical_blocks: int = 0


@dataclass(frozen=True, slots=True)
class JointCostConfig:
    """Age-conditioned restoration and route quotation model parameters."""

    horizons_seconds: tuple[float, float, float] = (60, 300, 1800)
    wear_weight: float = 1.0
    nand_seconds_per_gib: tuple[float, float] = (0.05, 0.2)
    storage_read_bytes_per_second: tuple[float, float] = (1e10, 1e10)
    storage_write_bytes_per_second: tuple[float, float] = (2e9, 2e9)
    gpu_bytes_per_second: float = 5e10
    prefill_tokens_per_second: float = 60000
    candidates_per_kind: int = 2
    scenario_limit: int = 8
    reclaim_bytes: int = 0
    sample_every: int = 8
    samples: int = 32
    seed: int = 42
    nand_page_bytes: int = 16384
    nand_pages_per_block: int = 256
    mapping_pages_per_block: float = 1
    overprovisioning: float = 0.125


@dataclass(frozen=True, slots=True)
class HybridAttentionConfig:
    """Opt-in backend replay with chunked or request-end checkpoint generation."""

    enabled: bool = False
    full_capacity_bytes: int = 0
    linear_capacity_bytes: int = 0
    full_block_bytes: int = 0
    linear_checkpoint_bytes: int = 0
    full_policy: str = "segment_lru"
    linear_policy: str = "checkpoint_lru"
    lru_k: int = 2
    alpha: float = 0.1
    decay_seconds: float = 60.0
    linear_read_cost: float = 1.0
    admit_storage_hits: bool = True
    dump_on_eviction: bool = True
    replay_mode: str = "exact_objects"
    tokens_per_block: int = 0
    checkpoint_interval_tokens: int = 4096
    full_drop_fraction: float | None = None
    linear_drop_fraction: float | None = None
    emit_io_trace: bool = True
    recompute_weight: float = 1.0
    tail_prior: float = 1.0
    drop_phase: float = 0.0
    pool_allocation: str = "shared"
    pool_decay_seconds: float = 300.0
    eviction_mode: str = "legacy"
    full_retention_ns: int | None = None
    linear_retention_ns: int | None = None
    linear_retention_policy: str = "idle"
    checkpoint_generation: str = "chunked_prefill"
    cost: JointCostConfig = field(default_factory=JointCostConfig)


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
    hybrid_attention: HybridAttentionConfig = field(default_factory=HybridAttentionConfig)
