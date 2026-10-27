# Age-conditioned joint memory policy progress

**Status**: 🔄 In Progress
**Started**: 2026-10-27
**Last updated**: 2026-10-27 12:10 UTC
**Current stage**: 2. Complete route quotations, physical cost and atomic plan execution

## TODO
- [x] 0. Freeze the shared-DRAM baseline and implementation contract
- [x] 1. Online age-conditioned demand estimator and joint recovery value
- [ ] 2. Complete route quotations, physical cost and atomic plan execution
- [ ] 3. Cost-driven shared DRAM victim and Drop/Dump selection
- [ ] 4. Functional, native, Python and device regression verification
- [ ] 5. Existing-dataset baseline comparisons and policy iteration
- [ ] 6. Documentation, reproducibility and requirement-by-requirement completion audit

---

## Detailed Log

### Initial state
Active goal: implement and iterate the discussed memory policy in DWPDSim, validate on existing datasets (cropped windows allowed), fail immediately on exceptions, and compare against baselines. User additionally requested saving current changes first and following thread 01a1784f-b658-7801-92ac-4b8fb9b5f91f for test methodology.

Primary checkout snapshot committed as ad50b9662c9d9f0ef2a32e2bab2615c07c2a3fef on feat/hybrid-attention-support. Ruff and Debug CTest 5/5 pass; pytest 444 passed, 2 skipped, 1 existing adaptive_endurance idle-threshold expectation failure. Primary checkout is clean. This regression belongs to that snapshot and was not copied into this branch.

Created codex/age-route-cost from main f2c20211c24982560186b989e9c85d9d4beb8e5d. Imported current source files from the reference worktree /home/solidyang/.codex/worktrees/dwpdsim-kimi-k3 (HEAD 001b06dc3de8b523ac889015aa94f8d2a2fef7e0 plus its finite-storage/shared-DRAM changes), without changing the reference checkout. Full source hashes are in ignored build/age-route-cost/baseline-source.json.

Required experimental contract: Kimi-K3, 100 GB shared DRAM with no FULL/LINEAR quota, finite 1 TB/4 TB SLC:TLC=1:2, joint restore tokens/input tokens, request-end prefill-only checkpoint publication, no invented decode hashes, empty-cache replay, same input and duration for all policies. Existing prepared workload: /home/solidyang/workspace/traceGen/runs/weka-subagent-24h-40tb-v1/dwpdsim (70,132 requests, subagent sharing/timing retained). Baselines use actual LRU-2 plus RR/adaptive storage, with infinite backing as a recovery upper bound. Compare physical costs through MQSim separately from logical host writes.

Scope remains the complete age-conditioned Drop/Dump cost with FULL/LINEAR recovery dependencies, finite-storage victim losses, tier/stream route effects and marginal media costs. No silent fallback, ignored errors, rollback scaffolding or Python per-block callbacks. Demand predictor validation is not evidence that the complete system policy works.

### [11:22] 0. Freeze the shared-DRAM baseline and implementation contract
Starting...
Baseline verified: 519 pytest passed, 2 optional skips; Debug CTest 5/5; Ruff passed. Three empty-cache replays use exactly 1584 requests in [0,1800), 100GB shared DRAM and one frozen native. RR1TB hit=0.9103116708701912, RR4TB/infinite=0.9177967326041864. 126 imported source hashes verified. Evidence and frozen native: build/age-route-cost/baseline-audit.json and baseline-native.so. Stage 0 complete.

### [11:27] 1. Online age-conditioned demand estimator and joint recovery value
Starting... Baseline source committed as 994f3a7. Imported MQSim patch context contains intentional leading diff spaces and tabs; do not rewrite patch contents to satisfy generic whitespace lint.

### Stage 1 component implementation checkpoint
Added cpp/include/dwpdsim/policies/memory/age_reuse.hpp and its C++ implementation. AgeReuseEstimator implements separate FULL/LINEAR age-first hierarchical shared tables (32/64/64/256 strengths), a distinct count-zero cold-publication group, mature-deadline labels, frozen issue-time features/predictions, equal-weight PAVA over three horizons, and explicit tail censoring. settle_before(now) must run BEFORE recording the current logical request; equal-time deadlines stay open until the timestamp advances. finish uses the actual observation end. No next-use arrays or cache-hit labels. The caller owns logical count/last/gap history; sampled forecasts read current count through a C++ callable. No Python hot-path callbacks.

Added hybrid_recovery.hpp/cpp: read-only per-copy delta view over the same RadixTree, no tree copies or rollback; deepest continuous-FULL + readable-LINEAR endpoint with N-1/max-prefix limits. Loads use allocated SSD bytes, payload GPU bytes and disjoint-pool bulk reads; GPU upload follows reads, then fixed-throughput suffix compute. This first time model is explicit and not measured TTFT. Queue/interference costs remain a separate future quote. Joint FULL+LINEAR deletion is evaluated once, including fallback endpoint changes.

New public module integration cases pass for current-age separation, uniform mature labels, same-time reuse, horizon-edge demand, censoring, independent component tables, joint restore A/B from FULL A/B/C + LINEAR B, shared loss without double counting, backed eviction/mixed media, read-only state and N-1 recovery limits. Current Debug CTest 7/7 passed. Rebuilt editable native; full Python regression is being checked before the next checkpoint.

IMPORTANT: These two components are compiled but NOT YET CONNECTED to the executor or a selectable cost policy. Stage 1 is still incomplete. Do not claim a new policy result from the frozen baselines. Next: add cost configuration, logical-demand observation (FULL all input blocks; LINEAR applicability independent of residency), bounded causal landmark sampling and historical joint request scenarios. Then read-only complete route quotation + exact commit, marginal media/GC/migration accounting, and shared-DRAM cost victim selection. Preserve full objective; do not replace route-aware physical estimation with size times average WAF, independently sum FULL/LINEAR losses, assume marginal block probabilities identify a joint request law, or silently fall back to legacy selection.

Baseline experiment location build/age-route-cost; primary checkout snapshot ad50b96; main-based development baseline commit 994f3a7. Reference checkout and its dirty changes remain untouched. .gitattributes exempts literal .patch context from whitespace lint without modifying the MQSim patch. No background benchmark process remains from the three baseline jobs.
Rebuilt-current-native Python regression completed: 519 passed, 2 optional-dependency skips in 3.50s. Ruff and current diff whitespace checks passed. Stage 1 remains in progress because logical-demand/scenario integration and selectable policy are not implemented yet.
Stage 1 integrated into prefix replay: raw FULL and applicable LINEAR observations precede cache actions, including misses; publication starts only cold count-zero history; uniform known-object landmark sampling and actual-observation-tail censoring. Common historical path prototypes fit nonnegative marked-Poisson hazard increments; next affected request scenarios are shared by all candidate plans. Six new public Python cases passed before the bulk-reclaim refinement. This demand-law approximation and finite scenario coverage require dataset validation, not a claim that marginals identify the true joint law.

### [11:57] 2. Complete route quotations, physical cost and atomic plan execution
Starting...

### Integrated policy and first real-data iteration
Implemented selectable hybrid eviction_mode=joint_cost (finite storage, shared DRAM, prefix_replay and LRU-K indices; incompatible profiles throw). JointCostConfig is forwarded through public Python/C++ APIs. JointMemoryCostPolicy observes all raw FULL inputs and every applicable published LINEAR prefix endpoint independently of cache residency; count-zero publication does not count as demand. Bounded uniform landmark sampling and finish censoring are live. Shared historical path prototypes, regularized nonnegative Poisson intensity fitting and first-affected-request masses form an explicit approximate joint demand law. No multiplication of block probabilities into a joint hit.

Complete candidate groups are cold LRU-K groups of at least the reclaim quantum (default one LINEAR checkpoint) and compete by cost per released byte. Quotes enumerate tier/stream and inherit an existing same-prefix bundle placement where required. Capacity quotes call existing read-only reclaim_for with earlier planned victims excluded; recursive destination reservation covers capacity migration. This is bounded current-leaf quote search, not exhaustive cascade optimization. Selected capacity actions and writes execute exactly once; insufficient quoted capacity or action failure throws, with no Dump-to-Drop downgrade. Existing storage background/access policies remain active.

Media quote uses marginal programs, mapping allowance (one page/data block), partial stream-frontier mixing and TRIM credits, migration destination programs and current-pool pressure; it is a model, not measured NAND WAF. New-copy survival uses pool-write-rate lifetime, one expected SSD return (DRAM hits do not refresh storage recency), actual idle ticks and common stratified draws. Default physical proxy currently uses 16KiB pages and 256 pages/block; reference MQSim uses 4224 pages/block, so formal device comparisons MUST pass matching model geometry rather than claim existing defaults match hardware.

Added tests/test_joint_cost.py: public baseline-vs-cost inputs/recovery oracle, raw logical demand after last-copy loss, tier-price route changes, finite trace address/dependency/byte ledger, trace-on/off determinism and pending-request checkpoint regression. Current Python suite 526 passed, 2 optional skips. Debug build succeeded; prior 7/7 CTest passed before the latest promise/prior change; rerun current Debug CTest before a final verification claim. Ruff clean. Current baseline RR1TB1800s stats exactly equal frozen baseline in every field (1584 requests, 91.031167087% joint hit).

V1 real-data cost run (same 1800s/1584 requests, candidates1/scenarios4) is terminal: 25.1304517231% joint hit, 398.521s. This is a failed diagnostic iteration, not a successful policy result. First bug: value ignored pending request-end checkpoints and upcoming FULL publications, undervaluing incomplete current prefixes. Fixed with quotation-only published/DRAM promises for remaining known request objects plus incoming admission. Actual hit tests still use actual published copies only. Added terminal-count path prior and shorter LINEAR endpoint templates. V2 is running via exec session 63715, directory build/age-route-cost/cost-rr-1tb-1800s-v2; latest 352 requests/105s showed ~59.22% hit, so more model iteration is needed, not goal completion.

IMPORTANT numerical/accounting issue to investigate next: media_quote currently charges full WRITE/relocation service time as queue cost. This may dominate expected reuse value even at wear_weight=0 and contributes to aggressive Drop. Correct C_queue should be incremental queued waiting/critical path beyond uncontended service, not the intrinsic write service time again. Proposed implementation: track per-pool busy_until from observed READ/WRITE; quote the whole action DAG twice (actual backlog vs empty queues), preserving migration READ->WRITE dependencies; charge only makespan difference, and account for copy readiness in future value. First save current V2 source/native so iteration remains attributable.

Performance issue next: value() repeatedly copies the common pending-publication delta into every plan/survival draw. Add a parent-layer HybridRecoveryState so the shared admission/promise projection is built once; apply only small plan and survival deltas. Cache identical Drop values per candidate group and skip duplicate no-write route quotes. This is a necessary quotation representation change, not a full tree/FTL snapshot.

Provenance: V1 old runner hashes the extension only at END; the live process loaded V1 before V2 pip replacement, so its recorded native SHA may be wrong. Correct diagnostic provenance using the already-completed cost-rr-1tb-60s-v1 native SHA (same V1 install), preserving raw result and clearly marking diagnostic recovery; do not claim a formal frozen binary for V1. New runner captures native SHA and copies the loaded-version binary at START, saves runtime-source hashes in provenance.json, reports end unchanged flags and Git HEAD. V2 has this new start provenance and native copy. No runtime source edits have occurred since V2 start (tests/progress are outside captured runtime sources). Preserve this fact or explicitly record changes.
