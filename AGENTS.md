# AGENTS.md

DWPDSim replays KV cache requests across memory and SLC/TLC tiers, producing
cache metrics and generic READ/WRITE/TRIM traces. Read [README.md](README.md) for usage
and [.design/rewrite-design.md](.design/rewrite-design.md) plus
[.design/segment-policy-design.md](.design/segment-policy-design.md) for detailed semantics.

## Architecture and invariants

- Keep the simulation hot path in `cpp/`. Python in `src/dwpdsim/` handles configuration,
  dataset adaptation, batching, and results. Large inputs use contiguous `uint64`
  buffers with `process_batch`; preserve equivalence with sequential `process` calls.
- `Simulator` coordinates transitions. `RadixTree` owns node state; `StorageState` owns
  capacity and addresses. Policies receive a read-only tree and decide without mutating
  shared state or outputs. Implement hot-path policies in C++; do not add per-block
  Python callbacks.
- `NodeId` is the globally unique input `hash_id`; parent links express topology only.
  The root uses an internal slot and reserves no input hash. Preserve repeated accesses
  and input order; timestamps are nondecreasing across calls and batches. Replay each
  simulator serially.
- Memory and storage policies choose radix segments. The simulator snapshots the global
  segment and operates on its selected-tier subset at block granularity. Memory blocks
  with SSD copies do not produce duplicate WRITE. SLC eviction demotes its selected
  subset with a TLC WRITE followed by the source SLC TRIM for each block; TLC eviction
  drops its selected subset with TRIM before the incoming WRITE.
- A node is pruned only when it has no memory or storage copy and no children. Deletion
  discards node statistics and policy-derived state; reappearance of the same hash starts
  a cold lifecycle with the same logical `NodeId`.
- Memory hits produce no I/O. Global misses enter memory without READ. Storage hits
  always READ before any promotion-induced eviction.
- A node may have a memory copy and at most one SSD copy. The built-in storage LRU uses
  one-way logical SLC-to-TLC demotion; it emits no migration READ and models no transfer
  latency. SSD internals, latency, write amplification, and DWPD belong in downstream
  adapters/analysis, outside the cache core.
- Stream trace output and call `finish()` before consuming it. Preserve deterministic
  metrics and event ordering; review output producers and consumers together when
  changing schemas. MQSim details and replay commands are in the README.

## Engineering rules

- Keep changes focused and preserve existing user edits. Use direct implementations;
  avoid speculative frameworks, duplicated state, snapshots, and unnecessary copies.
- Trust internal contracts. Validate necessary external boundaries: configuration,
  Python buffers, timestamp order, and output I/O. Fail clearly; avoid redundant checks,
  fallback/recovery branches, catch-and-continue handling, and transaction-style rollback.
  Use assertions only for concrete critical invariants.
- Log startup, configured aggregate progress, final metrics, and critical failures.
  Do not log individual accesses, evictions, or routine state transitions.
- Comments and design documents describe final implemented behavior and constraints.
  Replace stale statements; omit discussion history, rejected alternatives, and
  line-by-line narration of code.
- Keep configuration defaults and forwarding aligned across C++, bindings, and Python;
  export new public Python types through `src/dwpdsim/__init__.py`. Update relevant docs
  and consumers when public behavior, policies, or output schemas change.
- Write only module-integration and public functional tests using small deterministic
  scenarios. Check observable metrics and trace order; avoid private-method, trivial,
  defensive-branch, and coverage-only tests. Performance measurements go in `benchmark/`.
- Store generated traces, metrics, and build products under ignored `build/` or temporary
  directories. Do not commit generated artifacts, virtual environments, or datasets.

## Build and verification

Run from the repository root with Python 3.11+, a C++17 compiler, CMake 3.18+, and `uv`:

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```

After changing C++ sources, headers, bindings, or native build configuration, rebuild
before Python tests; the editable extension does not rebuild on import:

```bash
uv sync --extra dev --reinstall-package dwpdsim
```

C++ integration tests use Debug to keep their `assert` checks enabled. This separate
build does not update the extension installed in `.venv`:

```bash
cmake -S . -B build/cpp-tests \
  -DCMAKE_BUILD_TYPE=Debug -DDWPDSIM_BUILD_TESTS=ON \
  -DPython_EXECUTABLE="$(uv run python -c 'import sys; print(sys.executable)')" \
  -Dpybind11_DIR="$(uv run python -m pybind11 --cmakedir)"
cmake --build build/cpp-tests --config Debug
ctest --test-dir build/cpp-tests -C Debug --output-on-failure
```

Run affected checks: core/bindings need CTest and `tests/test_simulator.py`; Python
changes need Ruff and relevant pytest tests. `tests/test_mqsim_pipeline.py` checks
conversion only; use a small real MQSim replay when execution behavior changes.
Benchmark hot-path changes separately with a fixed dataset and configuration.
For documentation-only edits, verify paths/commands and run `git diff --check`.
