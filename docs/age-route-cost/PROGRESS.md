# Age-conditioned joint memory policy progress

**Status**: 🔄 In Progress
**Started**: 2026-10-27
**Last updated**: 2026-10-27 11:26 UTC
**Current stage**: 0. Freeze the shared-DRAM baseline and implementation contract

## TODO
- [x] 0. Freeze the shared-DRAM baseline and implementation contract
- [ ] 1. Online age-conditioned demand estimator and joint recovery value
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
