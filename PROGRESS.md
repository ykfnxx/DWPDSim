# Hybrid attention support progress

**Status**: ✅ Complete
**Started**: 2026-10-19
**Last updated**: 2026-10-19 02:53 UTC
**Current stage**: 6. Review regression and report

## TODO
- [x] 0. Baseline and implementation contract
- [x] 1. Opt-in hybrid core, component events and eviction
- [x] 2. Python/config integration, examples and benchmark
- [x] 3. Independent correctness tests and legacy regression
- [x] 4. Documentation, verification and delivery
- [x] 5. Compatibility and false-positive review/fixes
- [x] 6. Review regression and report

---

## Detailed Log

Branch: feat/hybrid-attention-support; base: 01251ab.
Existing hybrid design/test document edits are preserved.
Scope: synchronous backend replay; default-off feature flag; independent FULL/LINEAR states and pools; checkpoint LRU and benefit policy; exact object availability, independent test oracle; no production TTFT claims.
Base rebuilt; 28 legacy combinations captured before implementation.

### [02:32] 1. Opt-in hybrid core, component events and eviction
Starting...
Core compiled. 29 hybrid public tests pass. Legacy 28-combination snapshot identical.

### [02:38] 2. Python config, examples and benchmark
Starting...
Python event/columnar APIs, example flag/config and synthetic benchmark added.

### [02:40] 3. Correctness and compatibility verification
Starting...
Primary venv: 309 passed, 2 optional-dependency skips; Debug CTest 5/5 passed; baseline snapshots identical. Full test venv final rebuild running.

### [02:45] 4. Documentation and delivery
Starting...
Final source: 311 Python tests passed in full-dependency venv; primary venv 309 passed/2 optional skips; Debug CTest 5/5; Ruff; legacy 28-case snapshots identical. Thirty synthetic runs completed and documented; frequency/benefit did not outperform LRU on this append workload. Usage example executed. Changes remain on the requested implementation branch without commit/push.

### [02:48] 5. Compatibility and false-positive review
Review requested: preserve all default-off behavior; fix observed issues and add regression tests.
Confirmed legacy aggregate/copy compile regression; restored field order/value semantics and const-safe hybrid storage. Added 21 joint-availability tests; 65 hybrid tests passed against existing executor before rebuild.

### [02:51] 6. Review regression and report
Rebuild native extension and verify compatibility, const/copy contracts and zero false-positive cases.
Review completed: fixed legacy Node aggregate/copy regression and mutable hybrid state through const tree. Added 21 joint-ledger tests. Rebuilt both Python environments: 332 passed full; primary 330 passed/2 datasets skips; Debug CTest 5/5; 28 legacy snapshots identical in both; Ruff and whitespace checks passed. No reproduced false-positive hit within the supported synchronous identity model. Review report: report/hybrid-attention-review.md.
