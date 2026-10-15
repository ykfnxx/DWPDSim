# 基于历史复用分布的 Memory Policy 实验

`reuse_lru` 是实验策略，保留 indexed LRU 的 Radix segment 索引和 leaf 约束。它使用预先从过去时段拟合的 session 请求间隔生存曲线，不读取未来请求、task 或 agent-loop 标签，不改变原有策略的选择规则。

## 三个独立决策

- `reuse_dump_probability`：选中受害段后，以 `P(下一次 session 请求在未来 long_horizon 内到达 | 已闲置 age)` 判断是否 Dump，否则 Drop。为空时使用原有 retention 判断。
- `reuse_priority`：从最冷的合法段中累计覆盖 `alpha × memory_capacity_blocks`，优先淘汰近期返回概率最低的段，概率相同按原 LRU 顺序。关闭时使用 indexed LRU 选择。未提供前缀模型或代价开关时，这个值仅为返回概率。提供 `reuse_prefix_keep` 后，按实际会回收的尾部 block 计算条件复用概率；`reuse_cost_weighted` 再计入恢复代价。
- `reuse_admit_probability`：Storage 命中后，当前请求所属组的未来 short_horizon 返回概率达到阈值才回填；同时要求 `admit_storage_hits=true`。为空时完全由 admit_storage_hits 控制。

所有 reuse_lru 变体均只回收当前段尾部，使用同一 `max_eviction_blocks` 上限，不向父段回收。选段排序、Dump/Drop、Storage 回填可以分别消融。

## 模型与状态

模型 JSON 包含 `reuse_edges_s` 和 `reuse_survival`。边界从0开始严格递增，每行生存率从1开始单调不增。边界间线性插值，最后边界外保持常数，因此该范围外不预测额外返回概率。S(age)=0 时返回概率取0。没有前缀模型时，session 返回是 block 复用的代理。可选的 `reuse_prefix_keep[group][gap_bin][depth_bin]` 记录下一次请求保留该 block 的概率；gap_bin 对应相邻时间边界，depth_bin 是 `floor(3 * position / context_blocks)`，position 从0开始。训练通过同 session 相邻请求的最长公共前缀计算，未知后续请求不作为前缀失效样本。此模型统计历史失效规律，不直接识别某个旧分支已经失效。 block 年龄取其最近访问时间；session 返回却不再包含该 block 时，模型没有立即将旧分支失效，仍使用历史统计近似。共享 block 也只使用最近访问者，而非多个 owner 的联合需求。

九行按下面的顺序排列：

1. 此 session 首次被观察到 × 当前 context <=128 / 129–512 / >512 blocks；
2. 当前请求距同 session 上次请求 <=60s × 上述三种 context；
3. 当前请求距同 session 上次请求 >60s × 上述三种 context。

分组只依赖当前及过去已到达的请求。共享块的预测类别取最近访问它的请求；不是每个 owner 的联合预测。仅返回概率排序和条件写盘使用最近访问的内存成员的类别和年龄；前缀/代价排序逐个计算实际回收尾部的驻留成员，再取平均。session 时间戳保留到回放结束；node 类别仅随内存驻留维护。未实现 ghost 表、在线模型更新或 engine hints。

| 配置项 | 默认值 | 作用 |
|---|---|---|
| kind | baseline_lru | 设置为 reuse_lru 开启 |
| reuse_edges_s | [] | 模型时间边界，秒 |
| reuse_survival | [] | 九行生存曲线 |
| reuse_dump_probability | null | 条件写盘阈值，0–1 |
| reuse_priority | false | 按短期返回概率排序候选 |
| reuse_admit_probability | null | 选择性回填阈值，0–1 |
| reuse_short_horizon_s | 60 | 内存优先级及回填的预测窗口 |
| reuse_long_horizon_s | 3600 | Dump 的预测窗口 |
| reuse_prefix_keep | [] | 九组 × 时间区间 × 三个相对深度区间的保留率；非空要求 priority=true |
| reuse_cost_weighted | false | 按读取/重算代价代理加权；要求 priority=true |
| reuse_recompute_weight | 1 | 一个 block² 重算单位相对于单 block 读取单位的权重，必须为正 |
| alpha | 0.01 | 冷候选预算；本轮实验显式设置0.1 |
| max_eviction_blocks | null | 本轮实验统一64 |

`example/.env.example` 提供全部控制项，通过 `DWPDSIM_MEMORY_REUSE_MODEL_PATH` 读取模型 JSON。也可把模型键直接传给 `MemoryPolicyConfig`，使用 `benchmark/reuse_policy_experiment.py` 生成的 model.json。

## No-hint 动态软驻留

发生容量回收时，根据已等待时间 a，重新计算未来 h 秒的 block 复用概率：

`p = sum_k [(S(max(a, edge[k])) - S(min(a+h, edge[k+1]))) * keep[group][k][depth]] / S(a)`

只累计非空时间区间。返回概率与前缀保留率按 gap 区间结合，避免假定两者独立。
候选价值为 `sum(p * cost) / (候选回收 block 数 * h)`；block 等大，省略共同字节因子。
每次回收从冷候选集中选择价值最低的段，其他段继续驻留。没有独立定时器、固定到期清理或不可打破的保留承诺；空闲时不主动淘汰。内存压力通过何时需要回收及候选间的价值竞争体现，没有额外的压力预测器。

cost 的读取单位设为1。已有 Storage 副本，或按当前 Dump/Drop 规则淘汰时会写盘，使用读取代价1；无副本且会直接 Drop 时，使用 `reuse_recompute_weight * 最近访问上下文 block 数`。它是明确的代价代理，不是测得的SSD/推理延迟，也不计写盘代价和连续前缀的非线性依赖。

`retention_ns` 仍只决定受害段 Dump/Drop，不是本模型的驻留期限。动态软驻留通过改变 victim 选择影响实际驻留时间。`memory_performance()` 提供 `evicted_idle_bins` 和 `evicted_residency_bins`，区间上界为1、4、16、60、300、1800、7200秒及无穷，左闭右开；后者从每次插入 Memory 到回收计算，每个 block 驻留周期计一次，不包含结束时仍驻留的右删失周期。

```sh
build/test-venv/bin/python benchmark/residency_experiment.py prepare
build/test-venv/bin/python benchmark/residency_experiment.py run --workers 4
python benchmark/report_residency.py --output build/residency-nohint
```

该实验复用首轮准备的评估 Parquet 和仅供离线分析的标签。重新核对原始 JSONL SHA256，独立从前6小时拟合前缀保留率，并核对 session 生存曲线与首轮一致。少于50对请求的 gap 小组退回本组所有 gap，整个组不足50对时退回训练集总体。

两份数据 × 两个容量 × 六组配置，共24组：开启回填的dump-all基准、LRU/300s、仅返回概率、前缀复用概率、代价加权60s窗口、代价加权300s窗口。除dump-all外均固定300s Dump/Drop规则，回填全部开启、alpha=0.1、尾部64-block上限；不使用task或loop标签。仅返回概率组使用全1前缀表，确保与其他新排序策略的候选尾部口径相同。参数预先固定，测试集仅报告结果。

## 首轮复现实验

```sh
build/test-venv/bin/python -m pip install -e . --force-reinstall --no-deps
build/test-venv/bin/python benchmark/reuse_policy_experiment.py prepare
build/test-venv/bin/python benchmark/reuse_policy_experiment.py run --workers 4
```

脚本使用仓库已有的两份 canonical Parquet，核对对应原始 JSONL SHA256 后处理；路径见脚本 SOURCES。所有生成产物放在 `build/reuse-policy-experiment`。

训练使用 [0,6h) 的到达，最后一次观察之后的间隔在6h右删失；按组使用 Kaplan–Meier 估计，不把最后请求误标成永不返回。少于50个间隔的组使用训练池总体曲线。模型拟合后冻结。

评估使用 [6h,24h) 的所有请求，缓存和 policy session 历史在6h冷启动。不是全天冷启动结果，不与历史24h表格直接比较。两份数据、128/256 GiB、8 MiB/block、infinite_storage、finish不强制刷盘。每个任务和容量内共享同一训练模型与评估序列。

固定40组：每个数据/容量执行 dump-all、300s retention、条件写盘阈值0.15/0.35/0.60、仅优先级、全部回填、仅选择性回填、组合，以及原生 indexed_lru/300s。前三个机制的单项实验固定其他决策。组合阈值预设0.35，不按评估结果挑选。这里不宣称任何策略经过独立验证集调优。

## 指标

`enable_request_diagnostics(path)` 导出每个请求实际 memory/storage/global 结果计数，以及连续前缀 miss。不会改变访问和 I/O。实验以评估窗口内首次出现的 hash 计 cold blocks，`raw_misses - cold_blocks` 得到历史 block 丢失量。

loop 和用户边界仅用于离线归因。评估窗口内每个 session 的首次观察请求划为 first，不将训练区跨界间隔当成一次评估内续发。loop 内“历史内存 miss”包括之前 Dump 后的持续 Storage 读取；它不是一次新的淘汰事件。loop 内“历史全局 miss”是该请求之前出现过但所有层都无副本的 block，不包含首次出现的新增上下文，也不把首次全局 miss 之后所有后缀强行归为物理 miss。

写入减少和 compute cost 增加相对同数据同容量的 bounded-LRU dump-all；后者是 block² 代理，不是实际推理耗时。请求间隔包含模型及工具等待代理，不是纯 KV 空闲时间。没有推理完成事件、排队、SSD 延迟与写放大模型。
