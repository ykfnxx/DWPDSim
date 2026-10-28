# Joint-cost optimization progress

**Status**: ✅ Complete
**Started**: 2026-10-28 03:28 UTC
**Last updated**: 2026-10-28 05:17 UTC
**Current stage**: 4. Documentation and requirement-by-requirement completion audit

## TODO
- [x] 0. Diagnose and freeze acceptance
- [x] 1. Correct correlated storage survival and verify integration
- [x] 2. Paired policy iterations on unchanged prepared inputs
- [x] 3. Validate final configuration against LRU-2, indexed/context and infinite upper bound
- [x] 4. Documentation and requirement-by-requirement completion audit

---

## Detailed Log

User goal: continue optimizing the new policy; hit rate close to LRU-2, allocated host writes below indexed_lru/context_lru (both fixed300s); every comparison lists gap to infinite-storage joint-token hit upper bound. Existing primary experiment is [0,3600), 3127 requests, 506039360 tokens, 100GB shared DRAM, ~1TB SLC:TLC1:2, RR, one stream/pool, empty cache and request-end publication. Acceptance operationalizes close as >= LRU-2 minus0.1 percentage point, with strictly fewer allocated host-write bytes than both retention baselines. Preserve causal probability estimation, finite storage and complete executable routing; no future oracle or altered checkpoint publication. Verify small functional/device cases and a second longer interval before completion.

Previous goal-turn classification: progress. The previous work directly computed and cross-checked infinite-backing bounds: first hour94.3716884%, full day97.7163627%. Live artifacts and current worktree are authoritative; previous baseline/native snapshots remain frozen. Current source includes the completed hybrid context comparison changes, which must be preserved.

Initial diagnosis: value() independently removes individual SSD copies under survival quadrature; contiguous FULL recovery therefore collapses with path length, although storage victims are radix segments. Check segment-correlated survival first, then measured cost/reuse/candidate effects if needed. No subagents requested.

### [03:28] 0. Diagnose and freeze acceptance
Starting...
Frozen primary numerical gate: hit >=92.87783635%, writes below2,321,041,850,368B (and indexed2,349,747,666,944B). Hypothesis is segment-correlated survival; estimator, routes and data unchanged. Comparison driver now computes and emits its causal no-deletion upper bound.

### [03:29] 1. Correct correlated storage survival and verify integration
Starting...
544 pytest passed (2 optional skips), Debug CTest7/7, Ruff passed. Frozen correlation runtime includes matching source/native; snapshot startup disables only the installed editable redirect so its copied package/native are selected. Baseline and weight0 pair will run against that immutable snapshot while independent diagnostic work continues.

### [03:33] 2. Paired policy iterations on unchanged prepared inputs
Starting...

### Cached-survival and compute-priority iteration
The first correlated-survival replay exposed repeated segment_leaf_for traversal (per-node descending scan), producing quadratic work. It was intentionally terminated and marked aborted; no partial result is used. Replaced with exact per-prototype reverse-path cohort mapping; six synthetic public result sets are exactly equal to the uncached model. Added explicit compute priority through existing hybrid recompute_weight (default1), preserving hardware rates. 545 pytest pass, 2 optional skips.
Current frozen runtime: build/age-route-cost/optimization-v1/runtime-cached-priority. Main driver session97164 runs priorities1/4/16 at NAND weight0; indexed/context fixed300s validation sessions6531/41500 use the same frozen source/native. Absolute snapshot PYTHONPATH is required to override the installed editable redirect. Results under cached-priority-3600s. All compare summaries calculate upper bound after replay, outside policy/training.

### Longer-window reference preparation
Started same-frozen-runtime [0,7200) baseline/indexed/context reference runs, sessions92055/15214/35435; outputs build/age-route-cost/optimization-v1/validation-7200s/{baseline,indexed_lru,context_lru}. Once the one-hour priority screen selects a candidate, freeze its parameters and evaluate that candidate on the longer window. No inference/training consumes the reporting upper-bound calculation. Final current native Debug CTest7/7 and545pytest (2 skips) pass.

### Survival-model ablation
Added explicit cost.storage_survival modes: pressure (default, shared segment/pool quantile) and continuation (no inferred outside-pressure expiry under the fixed future continuation; quoted finite-capacity victims and known background expiry still apply). Actual finite storage execution is unchanged. Native bindings/dataclass/equality forwarding aligned. 549 pytest passed (2 skips), Debug CTest7/7 and real MQSim smoke with continuation/recompute4 passed (baseline223, joint395, indexed/context345 commands each).
Frozen continuation runtime: build/age-route-cost/optimization-v1/runtime-continuation; native SHA7026ea306bdfc8bb248a969e4e0940aa71778933d0091262e63e8b9674a5ce2b. One-hour driver session74294 runs baseline/indexed/context plus continuation priorities1/4 at media weight0; outputs continuation-3600s. Working C++ indentation cleanup was rebuilt and produced exactly the same native binary as this snapshot (format-binary-equivalence.json).
Offline optimistic final-hit bounds at current prefixes show all five screened candidates can still meet the first-hour target; no variants were pruned. This check is reporting/controller analysis only, not a policy input. Bound details: build/age-route-cost/optimization-v1/optimistic-bounds.json.

### Primary one-hour candidate passes
Continuation/media0/recompute1/candidates1/scenarios4 completed all3127 requests: hit93.0138683%, allocated host writes1,567,128,289,280B, compute35,352,576tokens. Hit is0.036032pp above LRU-2, writes33.306529% below indexed300 and32.481688% below context300. Gap to94.3716884% upper bound is1.3578201pp. Compared with original w0, hit+1.401352pp and writes-13.260465%. Source/native unchanged; first-hour proof saved in primary-candidate-proof.json. Other screened variants continue for ablation, not required to change the selected candidate.
Frozen final candidate uses runtime-continuation, native7026ea306bdfc8bb248a969e4e0940aa71778933d0091262e63e8b9674a5ce2b. Launched all-four-case [0,7200) validation driver session19293 at continuation-validation-7200s, with the exact selected parameters; same native for baseline/indexed/context/new. Do not mark goal complete before this validation and final artifact audit. Real MQSim selected-config smoke session47252 runs device-final-continuation-r1.

### [04:30] 3. Validate fixed candidate on the longer paired window
Starting...

### Primary screen and final static audit complete
All five one-hour survival/priority cases finished. Selected continuation/recompute1 dominates all screened alternatives on both hit and total host writes. One-hour same-native audit passed; raw demand/reuse training exactly equal to original w0, all legacy baseline stats exactly reproduced. Final current-source audit proves current code equals frozen sources apart from indentation and produces identical native binary; dataset file hashes rechecked. Final549pytest(2 skips), Debug CTest7/7 and Ruff pass.
Authoritative progress moved to docs/age-route-cost/optimization/PROGRESS.md. Main optimization report at docs/age-route-cost/OPTIMIZATION.md includes all completed comparisons with upper-bound gaps. Two-hour driver19293 remains live; actual OS parent2174526 and workers2174553(indexed),2174554(context),2174555(new). Latest checked: new4631/6192, index4951, context4279; all active at100%CPU. Continue waiting for this exact handle, do not restart. One-hour all-five parent74294 and pressure parent97164 have completed successfully.
Two-hour all-four same-native runs complete. Final audit passed: hit94.2722227%, LRU-2+0.9550795pp; allocated host writes2,955,410,407,424B, indexed-37.2777906%, context-35.9163477%; upper gap1.5982098pp. Both windows pass predeclared gates, first-hour serving outcomes exactly reproduce primary, and both bounds equal archived infinite native. Evidence: build/age-route-cost/optimization-v1/final-audit.json.

### [05:16] 4. Documentation and requirement-by-requirement completion audit
Starting...
Final report and README updated with both complete paired windows, every-row upper gaps, causal full-day bound and finite-capacity/pool-write caveats. Numeric report values and relative links checked. Final audit passed; both windows satisfy hit>=LRU-2-0.1pp and writes<indexed/context300s. 549pytest(2 optional skips), DebugCTest7/7, Ruff and real MQSim functional replay passed. git diff --check passed. No additional runtime changes or tests needed after documentation-only finalization. User-requested direct recalculation reverified 1h94.3716884%,2h95.8704325%,24h97.7163627%; complete24h new-policy experiment was not run or claimed.
