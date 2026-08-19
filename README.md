# DWPDSim

DWPDSim is a Python simulator for a hierarchical DRAM, TLC, and QLC storage
system. It consumes timestamped queries containing variable-length block ID
sequences and reports cache hit rates and I/O activity at each tier.

## Current model

- Every query is processed in timestamp order.
- Blocks within a query are processed sequentially and admitted to DRAM
  immediately, so a repeated block can hit later in the same query.
- TLC and QLC use exclusive placement: each persistent block resides in exactly
  one storage tier. DRAM may hold a cached copy.
- Initial blocks are seeded without simulated I/O. Referencing an unseeded block
  raises `BlockNotFoundError`.
- DRAM, TLC, and QLC capacities are hard limits measured in fixed-size blocks.
- Managers own state and enforce capacities. Injected policies only make
  admission, replacement, and placement decisions.
- `MemCachePolicy.on_remove()` decides whether a DRAM eviction is written
  to the next level. Managers execute and record the resulting I/O.

## Quick start

Install the package and development dependencies:

```bash
python -m pip install -e '.[dev]'
```

Run a simulation:

```python
from dwpdsim import DWPDSimulator, Query, SimulationConfig, TierConfig
from dwpdsim.policies import FrequencyPlacementPolicy, LRUPolicy

config = SimulationConfig(
    block_size_bytes=4096,
    dram=TierConfig(capacity_blocks=2),
    tlc=TierConfig(capacity_blocks=4),
    qlc=TierConfig(capacity_blocks=100),
)

simulator = DWPDSimulator.from_config(
    config,
    initial_blocks=(1, 2, 3, 4),
    memory_cache_policy=LRUPolicy(),
    storage_placement_policy=FrequencyPlacementPolicy(tlc_threshold=2),
    storage_cache_policy=LRUPolicy(),
)

report = simulator.run(
    [
        Query(timestamp=10, block_ids=(1, 2, 1)),
        Query(timestamp=20, block_ids=(3, 1, 4)),
    ]
)

print(report.metrics.dram_hit_rate)
print(report.metrics.tlc_hit_rate_on_dram_miss)
print(report.metrics.qlc_hit_rate_on_dram_miss)
print(report.metrics.io_counts)
```

Run the tests:

```bash
pytest
```

Run the complete example after installing the package:

```bash
python example/basic_simulation.py
```

## Replaceable policies

The built-in policies are:

- DRAM capacity handling uses the `MemCachePolicy` protocol.
- TLC capacity handling uses the independent `StorageCachePolicy` protocol;
  `choose_overwrite()` selects the block whose TLC position is overwritten.
- `LRUPolicy` and `FIFOPolicy` implement both protocols. Both write removed blocks
  downward by default; pass `write_back_on_remove=False` to drop them instead.
- DRAM admission: `AlwaysAdmitPolicy`
- TLC/QLC placement: `AlwaysTLCPolicy`, `AlwaysQLCPolicy`,
  `FrequencyPlacementPolicy`

Custom policies can implement the protocols exported by `dwpdsim.policies` and
be passed directly to `DWPDSimulator.from_config()`. A manager validates policy
decisions before mutating tier state.

With exclusive TLC/QLC placement, disabling write-back for a TLC overwrite
removes that block from modeled persistent storage. A later access to it will
therefore raise `BlockNotFoundError`.

## Package layout

```text
src/dwpdsim/
├── config.py       # Capacity and block-size configuration
├── models.py       # Queries, results, tier and I/O events
├── policies/       # Policy protocols and built-in strategies
├── memory.py       # Query-level DRAM manager
├── storage.py      # TLC/QLC manager and migration I/O
├── metrics.py      # Hit-rate, I/O, and capacity aggregation
└── simulator.py    # Top-level orchestration API
```

## License

DWPDSim is released under the [MIT License](LICENSE).
