# Kimi-K3 component cache / LRU-K delivery

Branch: `codex/kimi-k3-lru-k`, based on `main` at `f2c20211c24982560186b989e9c85d9d4beb8e5d`.
The original working tree is unchanged. This branch contains the implementation, tests, benchmark scripts and reports; generated artifacts remain local under ignored build/.

## Implemented behavior

- Pinned whole-model Kimi-K3 compressed MLA/KDA payload: FULL 27,648 B/token; LINEAR 449,372,160 B/checkpoint. Layout/dtype sources and configuration are in [the model guide](../docs/kimi-k3.md).
- Independent FULL and LINEAR residency, DRAM budgets, SSD replica accounting and LRU-K histories. Prefix recovery requires continuous readable FULL plus a readable LINEAR checkpoint at the same hash boundary.
- True-hit flags are false for single-component probes. The primary token metric is `prefill.joint_token_hit_rate = restored_tokens / input_tokens`.
- LRU-K uses distinct consuming requests, retains history over eviction/reload, and does not treat publication as reuse. Cold ties and K=1 behavior are documented.
- No-trace replay omits physical address vectors and per-request output lists, while preserving complete counters and object availability. Parent identity is checked during hybrid path construction; malformed input raises without rollback or recovery.

## Controlled no-trace benchmark

10,000 requests, 64 sessions (16 hot), 75% hot requests, continued two-block growth every fourth visit. Native hash size 16 tokens. FULL budget 512 blocks; LINEAR budget 16 states. Empty caches in every fresh worker. CPU 21, one warmup per variant, then three alternating measured rounds. Timing includes pybind batch marshalling and native replay; input generation and final statistics are outside the timer.

| Metric | Before optimization | Final implementation |
|---|---:|---:|
| Median replay seconds | 1.362886 | 0.903596 |
| Requests/s | 7337.37 | 11066.89 |
| Input token blocks/s | 1106968.62 | 1669630.21 |
| Peak RSS bytes | 63209472 | 54861824 |

Replay speedup: **1.508x**. RSS reduction: **13.21%**. Input hashes and complete simulation statistics match across all warmup/measured workers.

- Input SHA-256: `18886cc879312104643d801835dd4fa915f5188574c6230f09741948c1fd94fd`.
- Before native SHA-256: `3e3155cf908f126aa04a30f33079b5520f933e05b20e06dcec3cc3043b7b0eb2`.
- Final native SHA-256: `28d7e3d5c8549d6d5dad1bd9ee7ad5a83f93985423ca9207513b1961f46353fc`.
- [Complete configuration, counters, raw rounds and package provenance](../build/benchmark/kimi-final-ab.json).

```bash
.venv/bin/python benchmark/kimi_no_trace.py --requests 10000 --rounds 3
# With the retained pre-optimization package in this worktree:
.venv/bin/python benchmark/kimi_no_trace.py --requests 10000 --rounds 3 \
  --cpu 21 --baseline-package build/perf-before --output build/benchmark/kimi-final-ab.json
```

This measures simulator throughput, not model-serving throughput. RSS includes inputs/metadata and no real KV tensors. The measured revision uses infinite SSD backing and synchronous logical publication/reads. Finite-pool and MQSim integration is documented separately in docs/hybrid-attention-usage.md; these historical performance numbers do not measure that path.

## Verification

- Full Python suite: **493 passed, 2 skipped** (optional Hugging Face `datasets` dependency absent).
- Debug C++ CTest: **5/5 passed**, with assertions enabled.
- Ruff and `git diff --check`: passed.
- Independent public LRU-K oracle: K=1/2/3/5, Drop/Dump, FULL/LINEAR/both demands, repeated history wraparound and SSD reload.
- Integration coverage includes FULL-only/LINEAR-only misses, FULL middle gaps, cross-tier joint recovery, batch/scalar equivalence, generated-checkpoint rules, no-trace/trace counter equality and aggregate replay equivalence.
- Example environment forwarding for component policies and K=3 was executed successfully.
