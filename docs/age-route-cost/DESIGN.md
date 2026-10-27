# Age-conditioned joint Drop/Dump policy

`hybrid_attention.eviction_mode="joint_cost"` enables a native C++ experimental policy for a shared byte-budget DRAM cache and finite SLC/TLC backing. It uses the same input, publication and legal recovery rules as the LRU-K baseline. The source entry point is `JointMemoryCostPolicy::select` in `cpp/src/policies/memory/joint_cost.cpp`; `Simulator::hybrid_cost_evict` commits its selected plan.

## Objective and actions

For candidate DRAM victim set V and complete storage route r, compare:

```
J_drop(V)   = D(V) + Q_drop(V)
J_dump(V,r) = D(V) - B(V,r) + R(V,r) + Q_dump(V,r)
              + wear_weight * sum_t price_t * predicted_marginal_NAND_GiB_t
score      = J / actual_DRAM_bytes_released
```

D is the expected service-time increase after removing DRAM copies, retaining any existing SSD copy. B is the expected service recovered by adding the quoted SSD copies. R is the additional service-time change after applying the complete SSD capacity-victim and relocation plan. Each is calculated from the entire resulting FULL/LINEAR state, rather than by summing independent per-object replay lengths. Q is the change in expected request-visible queue wait relative to the unchanged state. Prices have units of equivalent compute seconds per NAND GiB. The generic prices `(0.05,0.2)` are explicit research assumptions, not measured hardware prices or a reproduced paper price.

Candidates are bounded cold LRU-K groups, separately drawn from FULL and LINEAR but competing in one DRAM pool. Each group releases at least one checkpoint's bytes by default, or `reclaim_bytes` when configured. Search covers Drop and every configured tier/stream; an existing same-prefix SSD bundle constrains the legal route. The actual storage policy quotes its capacity victims, including recursive destination reservation before relocation. Execution protects incoming writes, performs the exact quoted capacity actions, writes the chosen objects and releases DRAM. An infeasible route is excluded. A selected plan that fails execution raises an exception; it never silently changes to Drop.

Exact score ties prefer fewer last-copy bytes lost per DRAM byte. This preserves information without assigning a zero value to unrepresented future paths. No floating tolerance changes the objective. Candidate grouping and bounded prototype coverage make this a greedy approximation, with no global-optimality claim.

## Reuse probability and age

For each attention type, predict three residual-demand probabilities:

```
p_k(i,t) = P(at least one applicable logical demand in (t,t+H_k]
             | count, time_since_last_demand, last_gap, ancestor_history)
H = (60,300,1800) seconds by default
```

A cold published LINEAR checkpoint has a separate count-zero group, anchored at publication time. FULL demands follow every supplied input block, independently of cache residency or whether an earlier cache gap forces replay. LINEAR logical demand occurs at published endpoints applicable to the supplied path and N-1 prefix bound, independently of current copy availability. Publication itself is not reuse.

The shared estimator uses a Beta(1,1) global prior, then age/count-age/last-gap/ancestor hierarchical smoothing with strengths 32/64/64/256. Count has one cold plus eight positive log groups; age has 16 bins including zero; last gap has 16 bins plus unknown; ancestor has eight categories from up to 128 ancestors. PAVA makes same-query horizon probabilities nondecreasing. Default shared table storage is about 2.13 MB across both types. Per-object count/time/gap history, histories for lost copies, pending forecasts and transient quotations are additional memory; this is not a one-byte-per-object implementation.

Forecasts sample from all observed logical identities, rather than only eviction victims. Features and predictions freeze when issued. Both positive and negative labels train only when their deadline matures, before processing the next strictly later timestamp. Equal-timestamp later requests can count as zero-gap reuse. At the observation end, immature labels are right-censored. Brier sums and positive/sample/censor counts are reported per type and horizon. No next-use array or future test label enters the policy.

## Joint future-request law

Known terminal paths form bounded historical prototypes. A canonical hypothetical continuation supplies one additional uncached token after a known complete-block endpoint; it invents no hash or decode state. Nonnegative marked-Poisson interval intensities fit the forecast cumulative-hazard increments, with a historical terminal-count ridge prior. Prefix demands on one path occur together. Marginal forecasts alone do not identify a joint distribution: this marked process is an explicit model assumption, and `hazard_fit_squared_error` reports its mismatch.

Each prototype keeps its own first-arrival probability, rather than competing for one globally first request. For each nonzero interval, three conditional exponential quantiles integrate that arrival. Common shifted stratified draws place other prototypes' first arrivals. Under a fixed continuation, earlier requests repair/re-admit their FULL prefixes and publish their legal checkpoints, without further eviction. Later events see these repairs. This avoids repeatedly charging the same lost common prefix until it is repaired. All action quotes share the same events, arrival draws and SSD-survival draws. `expected_first_requests` is the sum of represented first arrivals over decisions and can exceed the number of decisions.

This finite-horizon continuation does not simulate future capacity pressure, further policy decisions or unobserved branches. Horizon truncation, prototype truncation, coarse quadrature and predictor error can affect results. Longer windows than 1800 seconds are needed to mature all default heads; short pilots primarily exercise cold-start behavior.

## Recovery and queue models

A valid recovery point x requires continuously readable FULL `[0,x)` and one complete readable LINEAR checkpoint at x. It is the deepest legal point within the request's N-1/max-prefix bound. Tree topology or a FULL hit never implies a LINEAR copy exists. Current-request future publications are quotation-only promises; actual hits always use the real published copies.

The service proxy adds bulk SSD read time, GPU upload time and suffix-prefill time. SLC/TLC bulk reads overlap through a maximum; GPU upload follows the reads and suffix compute follows upload. Default read bandwidth is 10 GB/s per pool, GPU bandwidth 50 GB/s and prefill throughput 60,000 tokens/s. It is a model of cache recovery service, not measured TTFT or a reproduction of the paper's overlap formula.

Actual trace I/O maintains one modeled FIFO busy-until value per pool. Streams share that pool's bandwidth. Relocation preserves explicit READ-to-WRITE dependencies. A quote schedules all its proposed operations on these queues. At a hypothetical request arrival, only pools actually read by its chosen recovery point incur wait. In-flight copies remain usable after waiting for the queued write; they are not reclassified as lost copies. Dump's own asynchronous completion wait is not charged as an immediate request stall. The logical executor remains open-loop; these times do not feed MQSim completions back into input timestamps.

## Survival and physical cost

Copy survival uses estimated storage invalidation lifetime based on current pool capacity/write rate and one expected storage return. DRAM-only logical hits do not repeatedly refresh SSD recency. Background idle eligibility follows the storage policy's tick schedule. Relocation updates write time but preserves storage last-access time. Public inspect fields prefixed `cost_model_` expose model times, not physical completion timestamps.

Marginal NAND estimates include new programs, migration destination writes, an amortized mapping term, projected GC copying, partial-frontier mixing effects on existing pages, and invalidation credits. Only bounded partial frontier summaries are copied during quotation; there is no full tree or FTL snapshot. The GC live-page approximation is `v/(1-v+OP)`. This is an analytical model requiring calibration, not measured WAF. Negative marginal contributions from invalidation are possible and should be interpreted as predicted credits.

Generic geometry defaults to 16 KiB pages, 256 pages/block, OP 0.125 and one amortized mapping page/block. The prepared-workload runner explicitly uses the reference MQSim geometry: 4224 pages/block and mapping term zero for `Ideal_Mapping_Table=true`. More streams create distinct frontiers but do not multiply capacity, bandwidth or endurance.

## Validation and experiment contract

Use `benchmark/prepared_hybrid_baseline.py` for paired, empty-cache runs. It saves exact config, dataset digests, all runtime source hashes and a copy/hash of the native binary before replay. Output directories must be new. Runtime source/native mutation during a run is detected. `baseline` selects shared LRU-K with unconditional Dump; `joint_cost` changes only the memory decision model and its route choice. The storage policy, capacities, input prefix, timestamps and publication convention must match within each pair.

Reference workload: `traceGen/runs/weka-subagent-24h-40tb-v1/dwpdsim`, 70,132 requests across 24 hours, 410 workflow instances and 30,551 subagent requests. Its timeline is a prepared experimental reconstruction, not a claim of original production global arrival times. Preserve the manifest's namespace and sharing. Every request publishes one prefill-end checkpoint at its final complete block; no decode hashes are inferred.

Reference model: Kimi-K3, 64 tokens/block, FULL 1,769,472 B/block, LINEAR 449,372,160 B/checkpoint; 100,000,000,000 B shared DRAM; finite 1/4 TB SSD with SLC:TLC logical capacity 1:2; baseline LRU-2. Joint token hit is `restored_tokens/input_tokens`. Report FULL/LINEAR separately, allocated host writes including padding/migration, replay tokens, predictor maturity and policy overhead. Do not equate host writes with NAND programs or extrapolate a cropped window to daily lifetime without an arrival/workload assumption.

`benchmark/joint_cost_device_smoke.py` runs identical synthetic requests through both policies, converts canonical traces and runs the local patched MQSim. It reconciles byte ledgers and dependencies. Its small logical pools reside inside 1 TB physical geometry; it proves functional completion only, not GC efficacy, finite-device WAF benefit or accurate latency. Physical measurements for a real workload must freeze geometry, OP, GC/WL settings, flow count, warmup and measurement window and report host bytes, NAND/GC programs, erases and completed commands.

Configuration and timestamp errors fail at public boundaries. External `HybridEvent` publication/demand is rejected for `joint_cost`; requests use `process_hybrid_prefill`, while observe events may advance background work. All hot-path estimation, quotations and state transitions remain in C++.
