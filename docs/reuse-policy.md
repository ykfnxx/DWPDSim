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

`example/.env.example` 提供全部控制项，通过 `DWPDSIM_MEMORY_REUSE_MODEL_PATH` 读取模型 JSON。也可把模型键直接传给 `MemoryPolicyConfig`，提供已完成拟合的 model.json。

## No-hint 动态软驻留

发生容量回收时，根据已等待时间 a，重新计算未来 h 秒的 block 复用概率：

`p = sum_k [(S(max(a, edge[k])) - S(min(a+h, edge[k+1]))) * keep[group][k][depth]] / S(a)`

只累计非空时间区间。返回概率与前缀保留率按 gap 区间结合，避免假定两者独立。
候选价值为 `sum(p * cost) / (候选回收 block 数 * h)`；block 等大，省略共同字节因子。
每次回收从冷候选集中选择价值最低的段，其他段继续驻留。没有独立定时器、固定到期清理或不可打破的保留承诺；空闲时不主动淘汰。内存压力通过何时需要回收及候选间的价值竞争体现，没有额外的压力预测器。

cost 的读取单位设为1。已有 Storage 副本，或按当前 Dump/Drop 规则淘汰时会写盘，使用读取代价1；无副本且会直接 Drop 时，使用 `reuse_recompute_weight * 最近访问上下文 block 数`。它是明确的代价代理，不是测得的SSD/推理延迟，也不计写盘代价和连续前缀的非线性依赖。

`retention_ns` 仍只决定受害段 Dump/Drop，不是本模型的驻留期限。动态软驻留通过改变 victim 选择影响实际驻留时间。`memory_performance()` 提供 `evicted_idle_bins` 和 `evicted_residency_bins`，区间上界为1、4、16、60、300、1800、7200秒及无穷，左闭右开；后者从每次插入 Memory 到回收计算，每个 block 驻留周期计一次，不包含结束时仍驻留的右删失周期。

## 验证

```bash
.venv/bin/python -m pytest tests/test_reuse_memory.py
```

模型拟合必须处理右删失，且仅使用决策之前可知的信息。生存曲线与前缀概率的接口允许
独立准备模型；当前策略不执行在线拟合。新 Kimi-K3 对象实验使用 [LRU-K 基线](kimi-k3.md)，
不将此固定曲线实验策略视为混合恢复代价模型。
