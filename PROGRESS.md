# Kimi-K3 component cache and LRU-K progress

**Status**: ✅ Complete
**Started**: 2026-10-26
**Last updated**: 2026-10-27 06:14 UTC
**Current stage**: 20. Gandalf experiment configuration and dataset matrix

## TODO
- [x] 0. Baseline and implementation contract
- [x] 1. Kimi-K3 FULL and LINEAR cache management
- [x] 2. LRU-K memory baseline
- [x] 3. No-trace performance measurement and optimization
- [x] 4. Functional verification, documentation and completion audit
- [x] 5. WEKA full-corpus preparation
- [x] 6. WEKA LRU-2 replay and result audit
- [x] 7. Verify and commit current implementation snapshot
- [x] 8. Finite hybrid storage and streaming MQSim integration
- [x] 9. Functional and device replay verification
- [x] 10. RR and adaptive endurance baseline matrix
- [x] 11. Results and limitations report
- [x] 12. Shared DRAM contract and global candidate selection
- [x] 13. Shared DRAM implementation and experiment configuration
- [x] 14. Integration, build and device verification
- [x] 15. Shared DRAM baseline evidence and documentation
- [x] 16. Qwen trace source and replay configuration
- [x] 17. Qwen shared DRAM replay and audit
- [x] 18. Qwen experiment results and reproduction
- [x] 19. Figure 7 experiment protocol audit
- [x] 20. Gandalf experiment configuration and dataset matrix

---

## Detailed Log

### Initial state
Created codex/kimi-k3-lru-k from main f2c20211c24982560186b989e9c85d9d4beb8e5d in an isolated worktree. Imported the existing component replay implementation and its functional tests as the iteration baseline. The original dirty worktree is unchanged. The current user requirements supersede historical compatibility requirements: no compatibility wrappers, no error recovery, no speculative boundary handling, no extra abstraction.

### [11:42] 1. Kimi-K3 FULL and LINEAR cache management
Starting implementation from the imported component executor.

### [11:55] 1-2. Component baseline and LRU-K
Implemented pinned Kimi-K3 payload profile and independent component indices. 21 new public functional/profile cases pass. User clarified true hits must be the FULL/LINEAR intersection; added joint token and restored-request rates.

### [11:55] 3. No-trace performance
Initial 10,000-request three-round baseline: median 0.440678s, 22,692 requests/s, 63,172,608-byte peak RSS. Optimization in progress; benchmark and native baseline are retained under build/.

### [12:12] 3. No-trace optimization complete
Three controlled alternating A/B rounds after warmup showed approximately 1.54x replay throughput and 13.6% lower RSS, with identical inputs and complete statistics. Final semantic tightening requires both components for exact-object true-hit flags; rebuilding and rerunning all gates.

### [12:12] 4. Completion audit
Checking all public APIs, config forwarding, source-pinned payload sizes, FULL/LINEAR intersection, LRU-K reference oracle, no-trace stats equivalence and reproducible performance evidence.

### [12:15] 4. Completion audit passed
All three requested outcomes are implemented and verified. Kimi-K3 source-pinned model arithmetic, independent FULL/LINEAR state, strict intersection true-hit flags and joint token metric are covered by public integration tests. LRU-K K1/2/3/5 passes an independent history/reference oracle. Final no-trace A/B: 1.508290x speedup, 13.2063% RSS reduction, identical input hashes and complete statistics; raw result build/benchmark/kimi-final-ab.json. Final Python suite: 493 passed, 2 optional datasets-dependent cases skipped. Debug CTest: 5/5. Ruff and diff checks pass. Config forwarding K3 verified. No new error recovery, rollback layer, compatibility wrapper or alternate topology. Original dirty worktree remains unchanged. Usage and scope limits are documented in docs/kimi-k3.md and report/kimi-k3-baseline.md.

### [12:24] 5. WEKA full-corpus preparation
Use complete downloaded WEKA 062126, preserve subagent root-relative times and trace-local shared hash identities. Align all trace starts at zero as an explicit reconstruction assumption. Prepare bounded-memory binary hashes plus sorted request index.

### [12:26] 5 complete; 6. Full replay
Prepared all 393 traces, 98,827 requests, 338,052,834 block references and 21,635,381,376 input-token slots. SHA-256 verified against pinned complete corpus. Preparation took 32.157s. Run K2, 80e9 FULL +20e9 LINEAR bytes, dump on eviction, infinite logical SSD, empty cache, request-end checkpoint, no I/O trace.

### [12:31] 6. WEKA complete
Full 98,827-request replay finished in 154.520s (native aggregate 138.805s), peak RSS 4,197,720,064 B. Joint restored tokens 21,167,276,032 /21,635,381,376 (97.8363896810%); restored requests 88,606; all-DRAM restored requests 159. Independent endpoint-set oracle matches all primary counts exactly. All 26 result invariants pass, prepared index audit passes, no trace files; Ruff and diff checks pass. Payload reads 255.548493 TB, writes 54.247889 TB; LINEAR is 81.3515% of write payload. Infinite SSD occupies 54.247889 TB, so not a finite-SSD/MQSim/WAF result. Corrected timing-scope text to include constructor, retained original result.raw.json and unchanged measured values. Reproducible script benchmark/weka_kimi.py; report report/weka-kimi-k3-lru2.md.

### [12:33] 7. Current snapshot verified for commit
User requested committing the current branch before discussing MQSim integration. Scope: all implementation, public tests, benchmark scripts and written reports in this isolated main-based worktree. Existing original-worktree and MQSim edits are outside this commit. Ruff and diff checks pass; previous native/full-suite and complete WEKA oracle evidence remains valid because simulator sources have not changed since those checks. Generated build outputs, datasets, binaries and virtual environment stay ignored. Local git commit follows this snapshot.

### [13:26] 8. Finite hybrid storage integration
Reuse actual RR/adaptive_endurance policy classes via a per-prefix SSD bundle view: same-prefix components share placement but retain independent byte sizes and availability. No second radix tree or Python hot-path callbacks. Fixed six NAND channels (2 SLC/4 TLC), 16KiB pages, 173/689 blocks per plane for nominal1/4TB, OP0.125, PE3/7; user-supplied media timing. First matrix is two capacities x two policies, same K2 memory baseline. Need bounded trace/conversion and device resource preflight before full runs.

### [13:50] 8-9 complete; 10. Baseline matrix
Finite hybrid executor now reuses actual RR/adaptive policies on same-prefix SSD bundles. Independent component sizes, addresses and joint recovery retained; capacity/idle TRIM and causal migration implemented. Added finite object trace v2 and bounded-memory streaming command conversion. Python497 passed/2 optional skips, Debug CTest5/5, Ruff/diff pass. Both policies pass small native->converter->real MQSim replay with host-byte/count reconciliation. Disk-backed host GMT snapshot b8a1dff built under build/mqsim-mapped (original MQSim untouched); original/mapped 1/2-flow hot-cold GC cases match exactly. RAM limitation is host metadata, not simulated capacity. Main matrix scope explicitly first1,000 requests of WEKA062126 (11.758s, cold cache), two nominal capacities x two policies, one flow per pool over2/4 NAND channels. All four DWP replays complete; RR1TB physical run in progress, adaptive1TB converted. Raw directories build/weka-storage-baseline/{rr,adaptive}-{1,4}tb-1000. This short window has no900s background tick; larger/full-corpus physical runs not claimed.

### [14:20] 10. First device case audited; remaining cases running/queued
RR1TB completed all1,131,945 commands in952.569s. SLC WAF1.0, TLC1.09640942, aggregate1.07938706; max block erases0/2, no PE limit reached. Independent Flow_ID, Pool_ID, completed/generated count, native/converted/device bytes and NAND=host+GC*16KiB audit passes. Adaptive1TB and RR4TB running; adaptive4TB queued to start after RR4TB exits to respect host RAM. All native results: joint26.5216/21.4235/33.9752/33.9752 percent. Added oversized-checkpoint false-hit regression and finite-hybrid example pipeline; final Python498 passed,2 optional skips. Example pipeline also passes real MQSim end to end. Report drafted with explicit cold-prefix/open-loop/bundle-placement/no-background-cycle limits; final physical tables await completion.

### [14:47] 10-11 complete. All four physical replays and independent audits passed
All3,470,635 commands generated and completed. Matrix order RR1TB/adaptive1TB/RR4TB/adaptive4TB: aggregate WAF1.0793870649/1.0783812952/1.0/1.0; max SLC/TLC erases0/2,3/0,0/0,0/0. Adaptive1TB completed normally with maximum SLC erase count at configured limit3; no run terminated for wear. Native/manifest/device byte reconciliation, geometry/OP/PE configuration checks and NAND=host+GC*16KiB conservation pass. Report report/weka-storage-baseline.md and machine summary build/weka-storage-baseline/summary.json complete. Short-window limits:1,000 requests,11.758s,zero900s background ticks,607gap samples below2048 q95-update cadence; no long-term ranking or TTFT claim. Next actual-timeline window suggestion documented using prepared index. Current integration changes remain uncommitted; no original-worktree/MQSim user edits changed. Final Python498 passed/2 optional skips, Debug CTest5/5 and real finite-hybrid example replay passed; Ruff/diff pass.

### [01:56] 12. Shared DRAM contract and global candidate selection
Starting removal of hard quotas and demand-derived soft targets from the new shared mode; preserve separate component identities and strict joint recovery. Current user correction supersedes old per-component capacity assumptions.
Contract: shared is the default, zero component capacity fields, aggregate byte budget only. Global candidate comparison supports homogeneous LRU-K and segment/checkpoint LRU; incompatible ranking pairs fail clearly. Old fixed/adaptive remain explicit historical modes. This corrects the shared capacity substrate; full Gandalf cost policy remains separate work.

### [01:58] 13. Shared DRAM implementation and experiment configuration
Starting...
Implemented default shared byte accounting and cross-component LRU-K / segment-checkpoint LRU comparison. Added explicit rejection of type caps and incomparable policy combinations. Kimi profile, WEKA runner, example config and metrics now expose shared mode; historical fixtures explicitly select fixed. Initial affected 58 public tests pass, including weighted independent K1/2/3/5 reference, true-hit loss, oversized state bypass and scalar/buffer/trace equivalence.

### [02:01] 14. Integration, build and device verification
Starting full Python suite, Debug CTest and small real MQSim replay.
Shared DRAM native rebuild, 519 passed/2 skipped Python tests and 5/5 Debug CTests passed. Real MQSim shared replays completed and reconciled 931 RR/1180 adaptive commands. Four controlled WEKA 1000-request runs verified shared cap zero, total peak <=100 GB, no targets and token conservation.

### [02:06] 15. Shared DRAM baseline evidence and documentation
Starting...
Updated shared defaults, runnable profile/prefill/env examples and explicit historical fixed reproduction commands. Added report/shared-dram-correction.md with four controlled WEKA results, aggregate cap evidence, unchanged joint recovery semantics, small real-device reconciliation, validation counts and Gandalf implementation limits. Ruff, diff check, all 4 documentation Python examples and all report links passed. No commit or push performed.

### [02:12] 16. Qwen trace source and replay configuration
Starting source location and schema checks for qwen_traceA_blksz_16; retain shared 100 GB, LRU-2 and existing dual-pool storage baselines.
Pinned official Bailian Trace A revision 5f7439c51ec248a0c585f7d90a41a6f57773b912 and LFS SHA256 07cedc9ed8aff301994ac68ed4aede8123b7603673575eeba9dd677de663db17. Prepared all 43058 requests/7199.464 s with 16-token complete blocks and exact partial-tail input lengths. Parent-aware interning produced 2616778 prefix nodes; 54810 raw hashes occur under different parents. Independent permanent endpoint oracle: 51829904/100382715 tokens, 51.63229945 percent. Model payload remains Kimi-K3, not inferred from trace name.

### [02:15] 17. Qwen shared DRAM replay and audit
Starting full-trace finite RR/adaptive 1 TB and 4 TB replays, plus permanent-storage control, each empty/shared 100 GB/LRU-2.
Paper comparison audit while full replays continue: Gandalf 2610.30791 Figure 7 x-axis is cache miss ratio, not hit rate. TraceA alpha=1 miss ratio 0.823 in section 5.9; ClaudeCode example 0.052 in section 5.2. Paper principal model/capacities are Kimi-K3, 100 GB DRAM and 1 TB SSD, so model name is aligned. Metric text is contradictory (miss ratio described as cache-served request fraction), requiring implementation evidence before comparison to our token-weighted joint rate. Existing two-pool SSD organization and classical LRU-K ranking remain different from paper policy.
Active execution bookkeeping: original sequential launcher PID 2046025/session 54398 is intentionally SIGSTOP while adaptive 1TB child PID 2046320 continues. Resource-aware parallel launcher PID 2046993/session 16841 runs adaptive 4TB child PID 2046994; it pauses 4TB if MemAvailable <600 MiB until 1TB finishes, then retires the original launcher only after both complete result files exist. All virtual timestamps/configs remain unchanged; elapsed timings may include resource pauses. The independent prepared-data/session audit passed; current report/gandalf-comparison-audit.md records the paper metric clarification.
Completed full TraceA results: infinite 51.6322994452 percent joint tokens (matches independent oracle exactly), RR 1TB 33.5198425346 percent, RR 4TB 47.4424785183 percent; all 43058 requests. Both adaptive jobs remain active. benchmark/qwen_bailian_kimi.py prepare/audit/run added; source/native core unchanged. report/qwen-bailian-traceA-shared.md currently lists adaptive as running and must be finalized with full stats/links after completion. report/gandalf-comparison-audit.md answers latest user question: paper Figure7 miss axes; default TraceA 0.823 versus ClaudeCode 0.052, Kimi/100GB/1TB nominally align; algorithms, 1MiB rounding, dual SSD pool, checkpoint supply and metric denominators not aligned. arXiv source.tar contains paper TeX/figures but no simulator code. Dense input audit verified all prefix labels, ancestry, times, lengths and session affinities.
Adaptive execution host affinity was pinned to CPUs 2 and 4 (different physical P-cores) without changing virtual timestamps or policy configuration. arXiv source v1 archive SHA256 6702c1f47b389dd693c3280f90a3abf9c0c847f9ebb8fec723a2497974311700 inspected; only TeX/PDF figures present. Figure10 writes-hit ratio has a different denominator from request/token hit rates; comparison audit updated.
Adaptive 1TB full run complete: 43058 requests, 33939520 restored tokens /100382715 =33.8101235855 percent; 17160 restored requests=39.8532212365 percent. All final invariants pass, 7 background ticks, 0 idle trims and migrations, host write 22891839094784 B. Elapsed2489.204857862s, peakRSS1559052288 B. Only adaptive4TB remains (about21760/43058 at2079s); original launcher stays stopped until resource-aware launcher retires it after both results exist. Report rows updated with finished adaptive1TB.
User refocused on unlimited SSD and challenged joint-hit semantics. Verified the exact A/B/C FULL + B LINEAR example through public APIs using the experiment native SHA in DRAM/mixed/SSD placements: all restore A/B (2 units), with no per-position LINEAR requirement. Independent whole-TraceA ledger reproduced 51829904 tokens exactly; historical FULL 58189168, checkpoint gap 6315968 plus N-1 cap loss 43296 across 96 requests. Removing only the N-1 cap diagnostically yields 51.6754303766 percent. Input-only supply omits decode FULL/checkpoints, so 51.6322994452 percent is not a demonstrated full-lifecycle infinite-store upper bound. Evidence under build/joint-boundary-audit and report/gandalf-comparison-audit.md; simulator sources unchanged. Adaptive4TB remains active at 41344/43058.
All five prefill-only TraceA cases completed and passed final count/token/capacity/IO audits. Adaptive4TB: 47.3571889344 percent tokens, 50.7431836128 percent restored requests, 47538432 restored tokens, 5422.195525653s. Independent detailed native full replay reproduces every original infinite-run final statistic; root and nonroot groups match independent ledger. Report scope remains prefill-only, not full-lifecycle serving.

### [03:58] 18. Qwen experiment results and reproduction
Starting...
Completed prefill-only full TraceA matrix, final invariants and report reproduction. Root/nonroot public native replay matches independent ledger and every original final statistic: root 40129205 input /2732832 restored tokens; nonroot 60253510 /49097072. All 19957 parent-child complete-input prefixes match. Figure9 verified live in paper section5.5: AIPerf, 400k requests with100 rounds per session,100GB DRAM+4TB SSD; it is not TraceA. Reports distinguish these workloads and preserve the unresolved decode supply and paper denominator limitations. All25 local report links, Ruff and git diff checks pass. Simulator code unchanged in this methodology audit. Both adaptive processes finished; original stopped launcher retired by orchestration as designed.

### [06:09] 19. Figure 7 experiment protocol audit
Starting...
Figure7 audit complete, not a reproduction claim. Confirmed paper LRU-K uses K-reference SSD admission; local LRU-K only ranks DRAM victims and defaults Dump-all. Full Gandalf cost admission is absent. Original infinite token miss48.3677 percent is not the1TB Figure7 case; local1TB RR token miss66.4802 percent. Paper requests/chunks denominator and checkpoint supply remain unconfirmed, with nominally same-configuration miss0.818 in section5.7 versus0.823 in section5.9. Report records evidence and limits. No simulator changes; report links and diff check pass.

### [06:13] 20. Gandalf experiment configuration and dataset matrix
Starting...
Verified paper experiment matrix against published text, figures and primary dataset pages: Figure7=Kimi-K3/100GB/1TB/all3 traces; Figure9=AIPerf/100GB/4TB/model sensitivity; Figure8=TraceA/fixed4TB DRAM+SSD, not4TB SSD. Table3 provides required DWPD at1/4TB but is not a policy hit matrix. Three trace names and primary sources verified. Main simulation1DWPD5yr/WAF1 differs from initialFEMU0.3DWPD validation. Exact lifecycle supply, miss counting and data timeline/generation remain unprovided by the paper. Added matrix to report; no simulator changes; diff check passes.
