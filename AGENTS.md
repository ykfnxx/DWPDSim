# AGENTS.md

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
- `src/dwpdsim/`: thin Python configuration and simulator facade.
- `benchmark/`: dataset-specific replay and performance workloads. Benchmarks are not
  correctness tests.
- `tests/cpp/`: C++ integration tests across core modules.
- `tests/`: Python functional tests through the public API.
- `example/`: small user-facing examples.
- `.design/rewrite-design.md`: implemented architecture and behavior specification.

Do not commit generated traces, metrics, build products, virtual environments, or
downloaded datasets.

## Required Simulation Semantics

The logical input request is an unsigned 64-bit timestamp and an ordered sequence of
unsigned 64-bit hash IDs. Each position is one block access, including repeated values.

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

Set up the Python development environment and run functional checks with:

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```

Build and run the C++ integration tests with:

```bash
cmake -S . -B build/cpp-tests -DDWPDSIM_BUILD_TESTS=ON
cmake --build build/cpp-tests
ctest --test-dir build/cpp-tests --output-on-failure
```

Run only the checks relevant to a documentation-only change. For implementation changes,
run the affected C++ integration tests and Python functional tests. Use a representative
dataset benchmark separately when changing hot-path behavior, memory layout, batching,
or trace throughput.

## Change Discipline

Preserve deterministic metrics and trace ordering for the same input, configuration, and
policies. Keep the generic trace schema simulator-neutral; add target-specific MQSim or
other conversions as separate adapters.

Keep changes focused and avoid unrelated cleanup. Update public documentation when
behavior, configuration, output schema, or supported policy choices change. Commit
coherent milestones without including generated artifacts.
