# 基于现有 RadixTree 的混合注意力缓存管理设计

状态：同步 backend 回放基础已实现；本文还包含未实现的协同扩展。实际支持范围见 [使用说明](hybrid-attention-usage.md)，尚无生产性能结论。

效果评估所需的模拟器改造、事件输入、对照实验与验收标准见 [混合注意力测试方案](hybrid-attention-test-plan.md)。

日期：2026-10-15。源码基线：DWPDSim HEAD `f2c2021`，同时参考当前工作区尚未提交的 memory policy 接口、`reuse_lru` 和相关文档。因此本文描述的是该工作区快照，不能仅凭 HEAD 复现全部参考实现。

## 1. 目标与边界

目标是在现有 block hash 序列、RadixTree、memory/storage policy 框架上，支持 FULL 与 LINEAR 混合注意力缓存，重点设计外部 backend DRAM 层的淘汰策略。

生产假设：

- backend 能观察当前请求的完整有序 block hash 需求序列；hash 包含前缀身份，或由接入层转换为等价的前缀唯一标识。
- backend 不理解 KV/state payload 的数值内容，但知道对象类型、位置、大小和副本状态。
- FULL 与 LINEAR 对应段的缓存状态分别表示，物理资源分别管理。
- 引擎决定 checkpoint 的生成以及哪些对象送入 backend。以 SGLang `write_through_selective` 为接入场景时，backend 接受上游供给过滤，不假定每个请求、每个 block 边界都有新 checkpoint。
- backend 管理自己的 DRAM 和存储副本，不直接决定引擎 GPU/Host 的淘汰。
- 得到需求序列不等于获得改变引擎恢复位置的权限。选择较早 checkpoint 并要求重算，属于需要额外协同接口的扩展。

本方案首先解决收到对象之后的驻留与淘汰；storage policy 仅定义与 memory policy 必需的决策边界，不在本阶段重设计 SSD placement、GC 或耐久算法。

### 必须满足的功能开关与命中约束

混合注意力通过拟定配置 `hybrid_attention.enabled` 显式控制，默认 `false`。省略开关与显式关闭等价，沿用全部既有 API、配置默认值、单池策略、miss 插入、存储迁移/后台行为、指标和 trace 语义。只有显式开启才进入分组件和双池管理；关闭时误传混合事件或非默认混合参数应明确报错，不得自动开启。新参数需贯通 Python、绑定、C++ 和 example/env 配置。

混合模式的命中必须以身份匹配、完整提交、当前可读的组件副本为依据。树路径存在、历史需求、ghost、pending 写入和部分 checkpoint 均不能代表命中。模式 A 只能满足实际请求的 endpoint；模式 B 回退后只计实际可恢复边界，不能将重放后缀计成缓存命中。联合命中必须同时满足同一前缀/边界所需的 FULL 与 LINEAR 数据，单组件命中不能代替联合命中。

所有策略共用权威可用性判定，policy 只影响保留与淘汰。独立参考账本按发布/完成/移除事件逐次核对，出现假阳性命中或旧行为回归时，不进入效果比较。完整验收规则见测试方案第 0、6 节。

## 2. 当前结构与算法依据

| 当前入口 | 当前行为 | 扩展重点 |
|---|---|---|
| `cpp/include/dwpdsim/types.hpp`：`Node` | 单套 `in_memory/on_storage`、存储地址和访问统计 | 按 attention 类型分离对象状态 |
| 同文件：`RequestContext` | `ordered_hashes`、request/session 标识和 protected prefix | 保留序列，增加组件需求/实际使用事件 |
| `cpp/include/dwpdsim/radix_tree.hpp`：`RadixTree` | hash 到 NodeId 的公共拓扑，提供 segment 查询 | 拓扑不复制；驻留查询按组件区分 |
| `cpp/include/dwpdsim/policies/memory/memory_policy.hpp` | 决策指定 leaf segment endpoint，mutation 只有 node id | 决策与通知增加组件身份和对象范围 |
| `cpp/src/policies/memory/indexed_memory_lru_policy.cpp`：`search/select_victim` | 排除有内存后代的 endpoint，按段 LRU 选冷候选 | LINEAR 不继承叶段限制 |
| `cpp/src/policies/memory/context_memory_lru_policy.cpp`：`select_victim` | 冷候选预算后，比较 endpoint 深度、驻留 block 数、LRU | 替换 LINEAR 二级排序的含义 |
| `cpp/src/policies/memory/reuse_memory_lru_policy.cpp`、`docs/reuse-policy.md` | 实验性复用概率、回填准入、Dump/Drop 和代价加权 | 可复用估计框架，不能直接复用 FULL 前缀保留概率 |
| `cpp/src/simulator.cpp`：`process_request` | 当前 global miss 自动代表计算新 block；compute_cost 使用 block² 代理 | LINEAR 需求、生产、供给必须分开；指标不能照搬 |

当前树的每个 Node 对应一个输入 block hash；segment 是拓扑上的连续无分叉路径，不是一个物理压缩节点内的 block 数组。扩展应沿用这一事实：各 Node 保存对应组件状态，段状态通过索引聚合。若未来压缩节点，再引入区间/bitmap，不在本轮强制修改树表示。

## 3. 共享拓扑，分别表示两类缓存状态

保留一棵通用 RadixTree：

```text
公共 block hash 前缀树
  Node hi
    common：hash、parent/children、深度、路径需求统计
    full：第 i 个 block 的 FULL KV 对象状态
    linear：前缀 hi 末端的完整 LINEAR checkpoint 状态（可不存在）

FULL DRAM pool    ← full 的 DRAM 引用
LINEAR DRAM pool  ← linear 的 DRAM 引用
Storage 管理器   ← 两类对象分别记录的 SSD 引用
```

一个 Node 的 FULL 存在不意味着 LINEAR 存在，反之亦然。没有 checkpoint 的边界仍可出现在需求树上。树分叉、段拆分不能生成新的 checkpoint。

建议的逻辑对象标识为 `(cache_namespace, block_hash, attention_kind)`。namespace 隔离模型版本、适配器、状态格式等不兼容内容；第一版假定同一 namespace 下每种类型聚合全部必要层。存在多组独立格式时再显式增加 group 标识，不能让同一个 hash 覆盖不兼容 payload。

组件状态包含：

- 对象大小、是否曾收到，以及当前是否有可用副本；“曾收到”用于历史统计，不等于可读。
- DRAM 引用和 SSD 位置，允许两层同时存在；SLC/TLC 继续属于 SSD 位置属性。
- 读写进行中、引用保护和提交完成状态；pending 写入不能当作已完成 SSD 副本。
- 组件自身的最近需求时间、最近实际读取时间、复用统计。

FULL 和 LINEAR 各自维护容量、占用及候选队列。第一版使用固定字节预算，两者之和不超过 backend DRAM 预算；自动调整两池配额是独立实验，不与淘汰算法同时引入。分池不要求占用不同物理 DRAM 设备。

节点剪枝必须同时检查两类组件及进行中的操作。两类数据均已消失且没有子节点后，才可移除结构节点。历史需求可以进入有界 ghost 表；不要求所有无数据节点永久留在树上。

## 4. 需求语义：路径经过不等于 checkpoint 被访问

对于请求路径 `[h1, h2, ..., hn]`：

- 路径事件用于建立共享前缀关系和观察匹配边界。
- FULL 组件需求表示该次恢复实际要求 backend 提供的 KV 区间。
- LINEAR 组件需求表示实际请求的 endpoint checkpoint，或协同接口明确允许的恢复候选。
- 实际读取、SSD 回填与对象写入分别通知，不能把一次写入视作一次需求命中。

如果只观察到完整路径而没有实际选中的恢复边界，只能估计潜在需求。此时必须将推断规则标记为模型假设，不能宣称得到了真实 checkpoint 访问序列。特别是请求最终长度不等于可复用 checkpoint 的位置。

示例：请求经过 `S2048 → S4096 → S8192` 并实际采用 `S8192` 时，只刷新被使用 checkpoint 的需求记录，不能把所有祖先 LINEAR 状态刷新为热。FULL 的需求统计仍按其实际所需区间更新。

shared checkpoint 的需求汇总来自所有实际使用者。仅使用最近 session 的返回概率不能代表多个使用者的联合需求；第一版优先使用对象级统计，session 模型只作为可消融的补充。

## 5. 淘汰粒度与合法候选

### 5.1 FULL

物理释放单位仍是一个可独立管理的 FULL block；当前 FULL 策略可继续按 segment 选择并从尾部批量回收，以保持对照实验的行为稳定。

外部 DRAM 在能够从 SSD 补齐区间时，并非物理上必须保持完整前缀。是否放宽 FULL 的叶段限制另做实验，本轮不把 FULL 策略变更混入 LINEAR 改进。

### 5.2 LINEAR

逻辑淘汰单位是一个 endpoint 的完整 checkpoint。在 payload 分片存储时，释放和可用性仍以完整恢复对象为单位；部分层重算不属于第一版。

候选集合为所有未被当前操作保护、允许释放的驻留 checkpoint，包括内部节点。不能继承 `has_memory_descendant` 的叶段条件。

删除内部 `S4096` 不要求删除后代 `S8192`，也不删除同节点 FULL payload。这里删除的是 DRAM 副本；是否下刷、是否丢掉最后一份副本由明确动作决定。

混合恢复时，两类对象的可用性必须一起检查。删除 FULL 副本可能增加某个 LINEAR 恢复点的使用成本，但无需立即级联删除 LINEAR；从 SSD 补齐 FULL 仍可能有价值。

## 6. 现有策略对 LINEAR 的适用性

| 机制 | 结论 | LINEAR 处理 |
|---|---|---|
| LRU 时间局部性 | 保留 | 使用实际 checkpoint 需求时间 |
| 冷候选筛选，再二次排序 | 保留 | 预算按 checkpoint 字节累计 |
| leaf segment 限制 | 移除 | 内部 checkpoint 也可淘汰 |
| 段内最近成员代表整段热度 | 不直接沿用 | checkpoint 独立统计 |
| 优先淘汰浅 context | 不直接沿用 | 改看需求与移除副本的边际成本 |
| block 数代表释放量 | 修改 | 使用实际 checkpoint 字节 |
| session 返回概率 | 有条件复用 | session 返回未必使用原 endpoint |
| FULL 前缀保留率 | 不能直接复用 | checkpoint 必须被选作恢复点才形成实际需求 |
| FULL block² 重算代理 | 不能直接复用 | 另建校准模型或报告重放长度 |
| retention、选择性回填框架 | 可复用 | 组件独立统计，分别消融 |

因此，现有算法可以提供索引维护和两阶段候选框架，但不能仅切换资源池就得到正确的 LINEAR policy。

## 7. LINEAR memory policy

### 7.1 基线：checkpoint LRU

所有合法 checkpoint 构成独立 LRU 队列，按最近实际需求时间淘汰。写入初始位置单独定义，不能重复发布一次就伪造一次复用。第一版采用普通 MRU 插入；扫描污染和 probation 准入留待后续消融。

该基线同时修正访问语义和叶段限制，是后续算法比较的必要对象。

### 7.2 改进：冷候选中的边际收益排序

1. 从最冷合法 checkpoint 开始，累计到 `alpha_linear × LINEAR DRAM 字节预算`；至少纳入一个对象，跨越预算的最后一个对象完整纳入。
2. 为各候选确定移除动作以及该动作之后的副本状态。
3. 估计保留该 DRAM 副本在有限窗口 H 内带来的收益，并除以实际释放字节。
4. 优先淘汰保留收益密度最低的对象；平分时按 LRU、对象 ID 稳定排序。
5. 执行后更新受影响的状态，再选择下一个对象。

目标定义：

```text
keep_value(c) = 未来窗口内，移除 c 的 DRAM 副本带来的预计请求成本增量
score(c)      = keep_value(c) / bytes(c)
淘汰 score 最小者
```

这是有限窗口的近似，不是精确最优缓存算法。第一版可用指数衰减的对象需求频率估计需求强度；衰减时间、窗口 H、alpha 均在验证集固定，不根据测试集未来调参。若采用“至少一次复用概率”，必须同时说明只近似下一次访问收益，不能把它当作预计访问次数。

### 7.3 有 SSD 副本：首先优化读取成本

已完成 SSD 备份时，固定恢复 endpoint 的近似评分为：

```text
score(c) = expected_demand_count(c, H)
           × max(SSD_service_cost(c) - DRAM_service_cost(c), 0)
           / bytes(c)
```

同大小、同读取成本的 checkpoint，绝对深度不是额外保留依据。深前缀的历史计算已被 SSD 副本保存，释放 DRAM 副本通常只增加读取成本。

读取成本可先用离线校准值；并行、合并读取和排队会影响请求完成时间，因此单对象成本是代理。没有延迟模型时报告读取字节和次数，不将评分代理当作真实 TTFT。

### 7.4 没有 SSD 副本：区分 Dump 和 Drop

- Dump：先提交存储写入，完成前保护源对象或将其转移到有预算的暂存区；随后释放 DRAM。动作成本还包含写入和等待，不能假设瞬间完成。
- Drop：不创建新副本。只有在不影响当前被保护请求、且系统允许该缓存对象丢失时执行。

首轮对照固定 Dump/Drop 规则和回填规则，只比较 victim 排序，避免把 storage admission 的变化归因于 memory 算法。

如果所有合法候选都被保护或写入中，应产生容量背压/推迟准入，不能突破预算或释放未完成 I/O 的源数据。

### 7.5 协同扩展：最后一份副本丢失的回退成本

仅在能确认引擎恢复语义、实际可用组件和重算模型时，加入：

```text
keep_value(c) = sum_q expected_count(q, H)
               × [best_cost(q, without_c) - best_cost(q, with_c)]
```

`best_cost` 必须考虑 FULL 区间、LINEAR checkpoint、各层读取和引擎允许的恢复位置。绝对深度不能替代此项。

示例：`S32768` 的前驱恢复点为 `S31744`，`S8192` 的前驱为 `S2048`；其他条件相近时，删除后者可能造成更大的重放损失。实际混合模型代价仍需校准，不能直接宣称按 token 数线性计时。

每丢失一个最后副本，相关恢复点的边际价值都可能变化；批量淘汰不能使用一次计算后不更新的旧分数。只有 DRAM 副本消失、SSD 仍可读时，也应更新分层成本，但不能将对象误判为全局丢失。

## 8. 请求、写入与回填流程

```text
ObserveRequest(path)
  更新公共需求拓扑；不生成 payload，不自动刷新祖先 checkpoint

ObserveComponentDemand(kind, keys/endpoints)
  更新组件需求；查找可读副本并保护本次操作对象

ReadComplete(kind, key)
  记录实际读取；按组件回填规则决定是否长期驻留

ObjectPublish(kind, key, size)
  表示上游实际供给；执行准入、分配和提交

PoolPressure(kind, required_bytes)
  从对应候选集合选对象 → 确定 Drop/Dump → 执行 → 提交 mutation

StorageWriteComplete(kind, key)
  发布可读 SSD 副本；解除对应 pending 保护
```

需求缺失不能自动生成 LINEAR checkpoint。当前 FULL global-miss 自动插入路径可保留为兼容的 FULL 回放模式；混合回放必须用显式供给事件或标明假设的生成模型。

在 `write_through_selective` 场景，需求事件与发布事件分别回放：未达到上游准入条件的对象可能有需求元数据但没有 backend payload；这种缺失不能算作 backend 淘汰导致的损失。

## 9. 当前接口的扩展方向

- `Node`：公共拓扑/需求统计之外，增加 FULL、LINEAR 两套组件状态。段内驻留量按类型聚合。
- `RadixTree`：共享拓扑查询保持不变；内存/存储后代和驻留计数增加类型参数，LINEAR victim 筛选不使用后代限制。
- `MemoryMutation`：增加组件标识；区分需求观察、payload 插入、读取、移除和备份完成，避免写入与访问混淆。
- `MemoryEvictionDecision`：不再强制所有类型返回 `leaf_segment_endpoint`；明确 FULL segment/range 与 LINEAR checkpoint 两种范围。LINEAR 不执行父段贪婪回收。
- `MemoryPolicy`：两类策略由同一协调层调用；保留只读视图、返回决策、commit 后更新索引的模式。
- `Storage`：地址和容量以实际字节/分配单元计量；同 hash 的两类对象拥有独立位置。多个物理块组成 checkpoint 时需完整发布可用性。
- `Simulator`：继续作为唯一状态修改者。分离需求、供给与完成事件，按组件产生 I/O 和指标。
- 配置：默认 `hybrid_attention.enabled=false` 保持全部既有行为；显式开启后使用两池字节预算、LINEAR policy、alpha、需求衰减/窗口和成本模型选择。混合结果使用独立统计接口/命名空间，不修改旧模式 stats/trace 契约。

以上为接口方向，不预先引入复杂插件框架或复制树、地址分配器。

## 10. 分阶段验证

### 阶段 A：结构和语义

验证同一 hash 的两类状态独立，容量分别守恒；FULL-only 回放保持原行为。覆盖内部 checkpoint 删除、树分叉不生成状态、SSD 写入未完成、请求保护、两组件同时消失后的剪枝。

### 阶段 B：固定供给与存储决策，比较 LINEAR 淘汰

基线与消融：

1. 直接迁移 FULL 叶段/路径刷新语义，作为明确标记的错误语义对照。
2. checkpoint 级 LRU：正确 endpoint 访问、内部点可淘汰。
3. 只修正访问统计或只放宽候选集合，分离两项收益。
4. 冷候选 + 需求频率。
5. 冷候选 + 需求频率 × 读取成本 / 字节。
6. 具备协同证据后，再比较回退成本扩展。

所有组固定两池容量、对象供给、上游 selective 规则、Dump/Drop 和回填；FULL policy 保持相同。初始预装 SSD 或 dump-all 可以用于隔离 DRAM 排序价值，但必须标为实验条件。

### 阶段 C：工作负载覆盖

覆盖持续追加、共享前缀分叉、历史编辑/回退、一次性扫描、冷热会话混合、不等 checkpoint 大小、SSD 拥塞和两池压力不均。

现有 hash trace 只能确定前缀需求关系，不能证明真实 checkpoint 生成或命中。缺少生产事件时，必须公开合成的 checkpoint 位置、供给延迟、上游准入和恢复规则，并做敏感性分析。

### 指标

- 分组件请求命中、对象命中、DRAM/SSD 字节和副本驻留时间。
- SSD 读取次数/字节、逻辑写入字节、等待写入占用和背压。
- 可组成完整混合恢复点的请求比例；不能用两组件命中率平均代替。
- 区分上游未供给、首次出现、backend 淘汰后丢失。
- 有恢复模型时报告重放长度；有执行校准时报告预计计算时间；真实部署再报告 TTFT 分位数。
- 有 MQSim/设备数据时报告物理写入与写放大；不能由逻辑写量直接声称耐久收益。
- 策略 CPU 时间、候选扫描数、元数据/ghost 开销。

验收先要求语义正确和容量/I/O 守恒，再判断相同资源预算下是否有收益。允许实验结论是 checkpoint LRU 已足够，复杂评分没有稳定增益。

## 11. 本阶段的设计结论

继续使用 block hash 序列和公共 RadixTree；FULL、LINEAR 对应段的状态、资源池和热度分别管理。FULL 保持现有策略作为对照，LINEAR 使用 checkpoint 粒度、允许内部候选，并按真实 endpoint 需求更新热度。

算法从 checkpoint LRU 起步，扩展为冷候选中的边际收益排序。有 SSD 副本时首先比较读取收益；丢失最后副本时才讨论经接口和模型支持的恢复损失。绝对 context 深度不作为 LINEAR 的通用优先级。

后续实现前需落实的输入契约是：实际 checkpoint endpoint、对象发布/完成事件、对象字节数，以及是否支持改变恢复位置。它们决定可验证的算法范围，不改变上述共享拓扑和分组件管理原则。
