# AGENTS.md

## Working in This Repository

These instructions apply throughout the repository. Run the commands below from the
repository root unless a command explicitly changes directory.

Start with `README.md` for public usage and `.design/rewrite-design.md` for architecture,
then inspect the relevant implementation and integration tests. Run `git status --short`
before editing and preserve existing user changes. If documentation disagrees with code,
identify the discrepancy before changing an observable contract.

## Project Purpose

DWPDSim replays KV cache block requests observed by a vLLM KVConnector. It models
block residency and eviction across memory and two peer SSD media, SLC and TLC. It
produces cache metrics and a generic READ/WRITE/TRIM trace for later conversion to
MQSim or another SSD simulator.

The simulation core is C++17. Python is the public interface and is responsible for
dataset parsing, batching, configuration, and result handling.

## Repository Layout

- `cpp/include/dwpdsim/` and `cpp/src/`: radix tree, simulation state machine,
  policies, storage address management, metrics, trace writer, and pybind11 bindings.
- `src/dwpdsim/`: thin Python configuration and simulator facade, plus the MQSim adapter.
- `scripts/mqsim_pipeline.py`: CLI entry point for trace conversion and MQSim execution.
- `scripts/analyze.py`: downstream physical-write and DWPD estimates using supplied
  write-amplification factors.
- `benchmark/`: dataset-specific replay and performance workloads. Benchmarks are not
  correctness tests.
- `tests/cpp/`: C++ integration tests across core modules.
- `tests/`: Python functional tests through the public API.
- `example/`: small user-facing examples.
- `example/mqsim/`: small SLC and TLC configurations for pipeline verification, not
  calibrated SSD models.
- `.design/rewrite-design.md`: implemented architecture and behavior specification.
- `pyproject.toml`, `uv.lock`, and `CMakeLists.txt`: Python dependencies, extension
  packaging, and native build/test targets.

Do not commit generated traces, metrics, build products, virtual environments, or
downloaded datasets. Put local run output under the ignored `build/` directory or a
temporary directory; root-level CSV and JSON output is not ignored automatically.

## Required Simulation Semantics

The logical input request is an unsigned 64-bit timestamp and an ordered sequence of
unsigned 64-bit hash IDs. Each position is one block access, including repeated values.
Timestamps must be nondecreasing across all calls and batches; equal timestamps preserve
input order. Normalize source timestamps in Python without sorting or deduplicating
requests to hide ordering errors.

The radix tree is a prefix tree whose node identity is `(parent_node_id, hash_id)`.
The same hash under different parents represents different blocks. The tree is the
authoritative owner of prefix relationships, per-node statistics, and residency state.

For each block access:

- A memory-resident node is a memory hit and generates no SSD I/O.
- A node absent from memory but resident on SLC or TLC is a storage hit and generates
  a READ. The memory policy decides whether it is promoted.
- A node absent from all media is a global miss. Its computation is considered complete,
  and the block is admitted to memory without generating a READ.
- A memory victim with an existing storage copy is removed from memory without another
  WRITE.
- A memory victim without a storage copy is either dropped or persisted according to
  the memory policy. Persistence invokes the placement policy to select the medium and
  stream.
- If the selected SSD medium is full, storage eviction emits TRIM before the incoming
  WRITE and reuses the released logical address.

SLC and TLC are independent peer media. There is no automatic migration or tiering
between them. A node may have a memory copy and one SSD copy, but it cannot have copies
on both SLC and TLC.

DWPDSim does not model SSD garbage collection, pages, erase blocks, internal data
movement, device latency, queues, completion events, write amplification, or DWPD.
Those belong to downstream SSD simulation and analysis.

## Architecture Boundaries

`Simulator` is the only workflow coordinator. It determines access results, invokes
policies, applies state changes, and updates metrics and trace output in deterministic
order.

`RadixTree` owns nodes and node state. `StorageState` owns capacity and logical address
allocation. `MetricsCollector` accumulates counters as transitions occur. `TraceWriter`
streams the generic CSV trace and must not retain the complete trace in memory.

Policies make decisions but do not directly mutate the tree, storage state, metrics, or
trace:

- `MemoryPolicy` controls storage-hit admission, memory victim selection, and drop versus
  persist behavior.
- `WritePlacementPolicy` selects SLC or TLC and the SSD stream for a persisted block.
- `StorageEvictionPolicy` selects a victim within the target medium.

Add a new hot-path policy in C++ and expose its configuration through pybind11 and the
Python facade. Do not add per-block Python callbacks or a general plugin framework.

Python dataset adapters must convert source records into contiguous `uint64` timestamp,
offset, and hash buffers and use `process_batch` for large inputs. Dataset-specific
schemas, timestamp normalization, and file formats stay outside the C++ core.

For `N` requests, all three buffers are one-dimensional; `timestamps` has length `N`,
`offsets` has length `N + 1`, and `offsets[0] == 0`. Offsets are nondecreasing and end at
`len(hash_ids)`. Repeated offsets represent empty requests. Keep batch processing
equivalent to sequential `process` calls. The bindings release the GIL during replay;
each simulator still processes requests serially, and callers must not mutate buffers
or access the same simulator concurrently during a call.

Public configuration changes may span `cpp/include/dwpdsim/config.hpp`,
`cpp/src/bindings.cpp`, `src/dwpdsim/config.py`, and `src/dwpdsim/simulator.py`.
Keep defaults and forwarding consistent, and export new public Python types through
`src/dwpdsim/__init__.py`. The runtime package has no declared Python dependencies;
NumPy is a development/benchmark dependency, and the batch API accepts compatible
buffer-protocol objects.

## Output Contracts

The generic CSV schema is emitted by `cpp/src/trace_writer.cpp`; the metrics dictionary
and `trace.schema_version` are exposed by `cpp/src/bindings.cpp`. Review both producers
and their consumers in `src/dwpdsim/mqsim.py`, `scripts/analyze.py`, and functional tests
when changing output fields or meanings.

- Trace `sequence` increases per I/O event. Events caused by one block access carry the
  same input timestamp. A storage-hit READ precedes any promotion-induced eviction I/O.
- Generic trace offsets and lengths are bytes. Stream IDs are scoped to their medium;
  a hash ID alone is not a unique block identity.
- `storage_hit_rate` is storage hits divided by memory misses. Other access rates use
  total block accesses as the denominator; rates with zero denominators are zero.
- `created` counts global-miss computations, including recomputation of an existing
  nonresident node. `tree.nodes` counts unique prefix nodes and excludes the root.
- Core timestamps retain the configured unit. `time.duration` is the last request
  timestamp minus the first, not wall-clock runtime or SSD execution time.

Create output parent directories before starting a simulation. Use the simulator as
a context manager or call `finish()` before reading or converting its buffered trace;
`stats()` and `write_stats()` do not flush the trace.

## Engineering Rules

### Keep the Design Small

Do not over-engineer. Introduce only abstractions required by current behavior or by an
explicitly requested replaceable policy. Prefer a direct implementation over generic
frameworks, registries, metadata systems, factories, compatibility layers, or future-
proofing without a concrete use case.

Keep state in one authoritative location. Do not duplicate node residency or statistics
across managers, and do not add transaction, rollback, checkpoint, distributed, or
parallel state machinery unless explicitly required.

Preserve the C++ hot path: use compact contiguous data where practical, stream input and
output, avoid state snapshots and unnecessary copies, and do not move block-level work
into Python.

### Do Not Use Defensive Programming

Internal modules and built-in policies trust their documented contracts. Do not add
fallback behavior, redundant validation, speculative recovery branches, catch-and-
continue exception handling, or transaction-style rollback for states that should be
unreachable.

Validate only external boundaries that are necessary for correct execution, including
invalid capacity configuration, malformed Python batch buffers, decreasing input
timestamps, and output file failures. Fail clearly when execution cannot continue.

Minimize assertions. Add an assertion only when it protects a concrete critical
invariant, such as storage address ownership or capacity accounting. Do not blanket
private functions and ordinary branches with assertions, and do not duplicate a check
already guaranteed by the caller's contract.

### Logging

Log only critical lifecycle and failure information:

- simulation configuration at startup;
- periodic aggregate progress when configured;
- final aggregate metrics and elapsed time;
- input-order or output-I/O failures.

Do not log individual block accesses, hits, evictions, policy choices, or routine state
changes. The trace and metrics are the source for detailed analysis.

### Comments and Design Documents

Unless explicitly requested otherwise, comments and design documents must describe only
the final implemented behavior and current constraints as facts.

Do not preserve discussion history, rejected alternatives, migration narration,
temporary decisions, iteration notes, or explanations of how the implementation evolved.
Update stale statements instead of appending amendments. Git history is the record of
change; source comments and design documents describe the current result.

Use comments only where they explain a non-obvious invariant, semantic choice, data
layout, or external format. Do not restate code behavior line by line.

## Testing Rules

Write only module-integration and user-visible functional tests.

C++ tests should exercise collaboration among the radix tree, simulator, policies,
storage state, metrics, and trace writer. Python tests should exercise the public API,
batch interface, generated metrics, and generated trace. Prefer small deterministic
scenarios that validate complete state transitions.

Do not add tests for private methods, trivial getters, implementation details, every
error branch, defensive checks, or assertions. Do not expand the test suite merely to
increase coverage. Performance and memory measurements belong in `benchmark/`, not in
pytest or CTest.

For behavior changes, cover the affected observable flow, including the relevant metric
and trace ordering. Important existing guarantees include shared-prefix identity,
memory/storage/global-miss paths, promotion and bypass, drop and persist, SLC/TLC stream
placement, no duplicate WRITE for an existing SSD copy, and TRIM-before-WRITE eviction.

## Build and Verification

Use Python 3.11+, a C++17 compiler, CMake 3.18+, and `uv`. The Python extension is built
with scikit-build-core and pybind11. Set up the development environment with:

```bash
uv sync --extra dev
```

After editing C++ sources, headers, bindings, or native build configuration, explicitly
rebuild the installed extension before testing through Python. The current editable
configuration does not rebuild native code on import:

```bash
uv sync --extra dev --reinstall-package dwpdsim
```

Run Python lint and functional checks with:

```bash
uv run ruff check .
uv run pytest
```

Build and run the C++ integration tests using the development environment's Python and
pybind11. Use Debug so the tests' C++ `assert` checks remain enabled:

```bash
cmake -S . -B build/cpp-tests \
  -DCMAKE_BUILD_TYPE=Debug \
  -DDWPDSIM_BUILD_TESTS=ON \
  -DPython_EXECUTABLE="$(uv run python -c 'import sys; print(sys.executable)')" \
  -Dpybind11_DIR="$(uv run python -m pybind11 --cmakedir)"
cmake --build build/cpp-tests --config Debug
ctest --test-dir build/cpp-tests -C Debug --output-on-failure
```

This CMake build does not replace the extension installed in `.venv`; rebuild that
extension separately when validating a core change through pytest.

Choose validation by the affected behavior:

| Change | Relevant checks |
| --- | --- |
| Documentation only | Verify referenced paths and commands; run `git diff --check`. |
| Core, policies, or bindings | Rebuild the extension, run CTest and `uv run pytest tests/test_simulator.py`. |
| Python facade or configuration | Run Ruff and `uv run pytest tests/test_simulator.py`; rebuild if native code also changed. |
| MQSim adapter or example SSD configurations | Run Ruff and `uv run pytest tests/test_mqsim_pipeline.py`; use a small real MQSim replay when execution behavior changes. |
| Packaging or changes across multiple modules | Rebuild and run the full Python suite and CTest. |

The MQSim pytest suite checks conversion without launching an MQSim executable. For
real replay, build the separate `../MQSim` checkout with `make -C ../MQSim` and follow
the README pipeline command with `--event-limit 100` and output under `build/`. Report
conversion checks and actual SSD execution separately.

For a small public-API smoke run with output kept out of the repository root:

```bash
mkdir -p build/example
(cd build/example && uv run --project ../.. python ../../example/basic_simulation.py)
```

Use `benchmark/swissai_baseline.py` separately for hot-path, memory-layout, batching,
or trace-throughput changes. Supply a local JSONL dataset with `created_at`, `bucket_ids`,
and `reused_buckets`, and set `--trace`, `--metrics`, and `--summary` under `build/`.
Record the dataset or subset, batch size, configuration, timestamp normalization, and
elapsed time when comparing runs. Do not substitute a full dataset replay for focused
correctness checks.

## Change Discipline

Preserve deterministic metrics and trace ordering for the same input, configuration, and
policies. Keep the generic trace schema simulator-neutral; add target-specific MQSim or
other conversions as separate adapters.

The MQSim adapter runs SLC and TLC as separate simulations. It maps each active
DWPDSim stream to one MQSim flow and compacts node addresses into that flow's logical
address partition. Keep simulator-specific workload XML and result parsing out of the
C++ cache core.

MQSim conversion uses 512-byte sectors and normalizes each medium's first event to
zero nanoseconds. Keep block sizes sector-aligned for this adapter, preserve
WRITE/READ/TRIM operation codes `0`/`1`/`2`, and respect the current limit of eight
active flows per medium. The local MQSim pipeline requires `PAGE_LEVEL` mapping and
disables device-level data caching for TRIM support. The example SSD configurations
are for pipeline verification, not performance calibration.

`scripts/analyze.py` estimates physical writes and DWPD from explicit SLC/TLC write-
amplification inputs and a positive trace duration with a supported time unit. Keep
these estimates distinct from counters measured by an MQSim run.

Keep changes focused and avoid unrelated cleanup. Update public documentation when
behavior, configuration, output schema, or supported policy choices change. Commit
coherent milestones without including generated artifacts.
