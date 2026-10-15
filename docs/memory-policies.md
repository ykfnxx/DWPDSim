# Memory Policy：indexed_lru 与 context_lru

两种策略都管理内存中的 KV block：内存不足时，决定淘汰哪一段，以及是否把数据写入 Storage。
`indexed_lru` 主要看最近使用时间；`context_lru` 在冷数据中进一步考虑 context 长度，并可调整保留阈值和淘汰粒度。

## 一、算法功能

| 功能 | indexed_lru | context_lru |
|---|---|---|
| 选择淘汰对象 | 选择最久未使用的可淘汰段 | 先找一批冷段，再优先选择 context 较短的段 |
| 决定是否写入 | 用固定 retention 判断 | 支持固定 retention，也可按 session 的 context 增量延长 |
| 淘汰范围 | 整段；Dump 在特定条件下可继续向父段回收 | 只处理当前段，从尾部开始，可限制 block 数 |
| Storage 命中后回填 | 由 `admit_storage_hits` 开关控制 | 相同 |

淘汰有两种动作：

- **Dump**：把没有 Storage 副本的 block 写入 Storage，再移除内存副本；已有副本不重复写。
- **Drop**：直接移除内存副本，不新写 Storage；已有的 Storage 副本仍保留。

**retention 是淘汰时的判断阈值，不是定时清理的 TTL。** 数据超过阈值后，不会立即被删除；只有被选中淘汰时才决定 Drop。关闭 retention 时默认 Dump。

## 二、总体结构

一次典型流程是：**处理请求 → 更新使用记录 → 内存不足 → policy 给出决策 → Simulator 执行淘汰。**

各部分的职责如下：

- **RadixTree**：保存共享前缀和 block 状态。算法按“段”选择数据；段可以理解为树中没有中间分叉的一段连续路径，末端称为 endpoint。
- **MemoryPolicy**：读取树，选择 endpoint、Dump/Drop 和淘汰范围，不直接修改数据所在位置。
- **Simulator**：执行移除、写入和节点清理，更新指标，再通知 policy 更新索引。
- **StoragePolicy**：需要写入时决定存储层的放置和回收；`infinite_storage` 实验模式省略这部分决策。

`indexed_lru` 维护三类索引：**block 的使用顺序、每段的内存成员、子树中的内存 block 数**。
访问、插入、移除以及树的分裂/合并发生时，增量更新这些索引，避免每次淘汰都重新遍历整棵树。

`context_lru` 继承这套索引和维护逻辑，替换候选选择，并增加可选的 session 增量、历史访问间隔记录。

## 三、算法原理

### 1. indexed_lru：先淘汰最冷的段

1. **判断哪些段能淘汰。** endpoint 下方不能还有驻留内存的后代，因此从内存数据的末端开始回收。
2. **按段的最近使用时间排序。** 一段的热度取其中最近访问的内存 block；只有整段都较久未使用，它才会排到前面。
3. **选择最冷的可淘汰段。** 默认搜索全部分组，得到全局最冷候选。可通过分组采样减少搜索量，但不再保证全局最冷；worker 只并行搜索，实际状态修改仍串行执行。
4. **决定动作。** `当前时间 − 段内最近访问时间 > retention` 时 Drop，否则 Dump；恰好等于阈值时仍 Dump。

选中后回收该段的内存 block。Drop 在当前段结束；Dump 若当前段没有需要新写的 block，可继续向父段回收，直到遇到需要写入的段或到达顶部。

### 2. context_lru：在冷段中优先选择较短的 context

它保留“先看冷数据”的前提，再做一次选择：

1. 从最冷的可淘汰段开始，累计这些段的内存 block 数，直到覆盖 `alpha × 内存容量（block数）`。最后一个段完整纳入，因此允许略超预算。
2. 在候选中依次比较：**endpoint 深度更小 → 段内内存 block 更少 → 更久未使用**；仍相同时用 endpoint ID 确定顺序。
3. 选中后只回收当前段，从深处的尾部开始，最多处理 `max_eviction_blocks` 个内存 block；未设置上限时处理整段。Dump 和 Drop 使用同一范围规则。

例如，两段都足够冷，一段 endpoint 深度为100，另一段为500，会优先选深度100的段。
设计动机是：重算代价通常随 context 长度增加。但当前实现是上述**逐项排序**，并没有直接计算或最小化 `block数 × context长度`。

`alpha` 控制候选范围，不参与最终分数，也不决定淘汰量。alpha 越大，能比较的冷段通常越多，搜索开销也越高。当前 context_lru 固定单分组、单 worker。

### 3. context_lru 的两个可选调整

**按 context 增量延长 retention**

```text
增量 ΔN = max(0, 同一 session 本次 context blocks − 上次 context blocks)
有效 retention = 基础 retention + beta × ΔN
```

beta 对应 `retention_growth_seconds_per_block`，单位为秒/block，默认0。
例如基础 retention=60s、beta=5、增量20 blocks，有效阈值就是160s。

增量在请求完成后更新到本次触及且仍驻留内存的 block；本次请求内部的淘汰使用已有历史。
首次请求、context 缩短或 session 标识为0时，增量为0。共享 block 使用最近访问它的请求记录；段的判断取最近访问的内存成员对应的记录。

**按历史访问间隔调整粒度**

```text
淘汰上限 = floor(基础粒度 × 历史访问间隔 / 参考间隔)
再限制到 [1, max_eviction_blocks]
```

由 `eviction_gap_reference_ns` 开启，基础粒度是 `eviction_base_blocks`。
这里的间隔是段内最近访问的内存成员在两个不同请求之间的 Memory 访问间隔，**不是它当前已经闲置多久**。
间隔越大，一次允许淘汰越多；没有历史间隔时使用基础粒度。同一请求内重复访问不刷新间隔，实际淘汰量还受当前段的内存成员数限制。

这两个调整默认关闭，也可独立启用。它们分别改变“是否写入”和“一次淘汰多少”，不改变 context_lru 的候选排序。

实现入口：[indexed_lru](../cpp/src/policies/memory/indexed_memory_lru_policy.cpp)、[context_lru](../cpp/src/policies/memory/context_memory_lru_policy.cpp)、[Simulator](../cpp/src/simulator.cpp)。效果与取舍见[实验报告](../report/context-lru-retention.md)。
