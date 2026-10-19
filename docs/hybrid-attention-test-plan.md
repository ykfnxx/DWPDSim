# 混合注意力缓存测试方案与 DWPDSim 最小扩展

状态：同步模式 A 已有基础实现与验证；本文保留后续模式 B/异步阶段的测试要求。实际支持范围见 [使用说明](hybrid-attention-usage.md)，尚无生产效果结论。

日期：2026-10-19。核对基线：DWPDSim `01251ab205346a683f0e90a6b3029fd33d2ebd31`，设计前工作区干净。算法依据见 [混合注意力缓存管理设计](hybrid-attention-policy-design.md)。

## 0. 必须满足的兼容性与正确性约束

### 0.1 配置显式开启，默认行为完全兼容

拟定唯一功能开关为 `hybrid_attention.enabled`，默认 `false`；名称和以下结构为待实现配置契约。未提供该配置与显式设为 `false` 必须等价。只有显式设为 `true` 才启用混合状态、双资源池、组件事件及 LINEAR policy。

```text
hybrid_attention:
  enabled: false                 # 默认，沿用全部旧配置与执行行为
  # enabled=true 时才配置 FULL/LINEAR 字节预算和 LINEAR policy
```

- 关闭时沿用原有单池容量、所有已支持 policy、Dump/Drop、storage placement/migration、后台 tick、global-miss 插入及指标定义；不能借本次扩展修改原策略。
- 关闭时旧 Python/批处理 API、默认值、返回结构、trace schema 和确定性事件顺序保持兼容；混合诊断放在开启后的独立接口/命名空间，不能改变旧 `stats()` 的字段含义或结构。
- 新开关和新参数必须完整贯通 Python config、绑定、C++ config、example/env 配置；不得按输入内容自动启用，也不得仅通过环境变量隐式启用。
- 关闭时提交混合事件，或配置仅适用于混合模式的非默认参数，明确报配置/接口错误；不得静默开启或忽略用户配置。开启时缺少必要的对象尺寸、两池预算或事件语义同样报错。
- 两模式只在配置决定的入口分流，共享已有基础设施。关闭模式不得因混合逻辑分配 LINEAR 池、改变节点剪枝与访问统计。是否共享内部字段不影响旧行为等价的验收要求。

### 0.2 命中由真实可用对象判定，策略不能创造命中

混合模式必须满足以下不变量，适用于所有正式策略和 P0/P1/P2 对照策略。对照只能改变候选或热度，不能修改统一的命中判定来制造差异。

```text
component_hit(q, o, t)
  => identity_matches(q, o)
     AND object_complete(o)
     AND readable_replica_exists(o, t)

joint_hit(q, k, t)
  => LINEAR checkpoint at the required boundary k is available
     AND every required FULL object through k is available
     AND all selected objects belong to the same compatible prefix/namespace
```

- 对象身份包含 namespace、组件类型、完整前缀 hash 和 checkpoint 边界；只比较深度、局部 block 内容、树节点存在或任一组件存在都不足以判命中。
- hash 沿用现有“前缀唯一身份”输入契约，不额外截断。外部输入若出现同一身份但 parent、格式或不可变对象大小矛盾，拒绝该输入，不能合并计命中。无 payload 模拟器的正确性结论以该身份契约成立为前提，不宣称能检测未知 hash 碰撞或数值损坏。
- 路径节点、ghost/历史需求、已分配地址、Publish 意图、pending 备份、部分 shard 或旧地址复用后的残留记录都不是可读副本。只有完整提交且尚未失效的当前对象可以命中。
- 查询可用性与服务完成分开：已提交 SSD 副本可以在查询时报告 storage-present；实际服务命中必须完成对应 READ，不能把查询成功、下发读取或等待写入计成已服务。
- 固定 endpoint 的模式 A 不能用较深或另一分支的 checkpoint 替代需求；模式 B 允许的较早回退只计实际恢复边界，后缀重放不能算成缓存命中。
- 联合命中针对同一请求边界的完整组件集合。不同层副本可组合，但必须在操作期间得到保护，不能拼接不同时间上从未同时可用的状态。
- 未知或不完整状态不得判命中；需要补全或拒绝输入时，单独记录，不用推测结果抬高命中率。

验收要求是：独立参考状态账本逐事件验证全部命中，出现一次假阳性即阻断效果实验。断言和高命中率都不能替代这项检查。

## 当前实施映射

同步首版以 `observe` 对应路径观察，`publish` 对应单组件供给，`request` 原子执行声明边界的全部组件需求与请求汇总，因此不需要独立 RequestEnd。支持 checkpoint LRU、频率和收益密度三种 LINEAR 策略；P0/P1/P2 迁移对照、有限存储、异步及模式 B 仍待实现。具体公开接口见使用说明，本文后续的事件名/扩展测试是设计契约，不代表全部已可调用。

## 1. 本轮要回答的问题

第一轮回答：在外部 backend 观察 block hash 请求序列、接收既定对象供给的条件下，LINEAR 专用淘汰是否优于直接迁移 FULL 策略和 checkpoint LRU？

具体区分三个问题：

1. 去掉叶段限制、只刷新实际需求的 checkpoint，是否减少不必要的 SSD 读取？
2. 在正确的 checkpoint LRU 之上，频率、读取成本、对象字节数是否提供额外收益？
3. 发生最后副本丢失时，回退距离和混合恢复可用性是否能改善保留决策？

问题 1、2 可以先通过 backend 对象回放验证；问题 3 需要引擎恢复模型。不能用第一轮命中率直接回答重算时间、TTFT 或端到端吞吐问题。

## 2. 为什么现有 DWPDSim 不足以直接测试

| 当前实现位置 | 当前语义 | 测试所需变化 |
|---|---|---|
| `cpp/include/dwpdsim/types.hpp`：`Node` | 一个 hash 一套内存/存储状态、地址与访问统计 | FULL/LINEAR 对应状态独立 |
| `cpp/include/dwpdsim/config.hpp`、`cpp/src/simulator.cpp` 构造和配置校验 | 全局 `block_size_bytes`，容量换算为等大 block 数 | 两个 DRAM 字节预算，分别配置对象大小 |
| `cpp/src/simulator.cpp`：`process_request/process_access` | 顺序访问路径，global miss 自动生成并插入内存 | 路径需求、组件需求、实际供给分别处理 |
| `cpp/include/dwpdsim/policies/memory/memory_policy.hpp` | 决策指向 leaf segment，mutation 只含 node id | 决策与提交通知包含组件类型；允许 checkpoint 单对象范围 |
| `cpp/src/policies/memory/indexed_memory_lru_policy.cpp` | 有内存后代的 endpoint 不可淘汰 | LINEAR 的内部 checkpoint 可成为候选 |
| `cpp/src/policies/memory/context_memory_lru_policy.cpp` | 冷候选按绝对深度、block 数、LRU 排序 | 增加正确的 LINEAR 基线和收益排序 |
| `cpp/include/dwpdsim/storage.hpp`、`trace_writer.hpp` | 地址分配、I/O 长度依赖统一 block 大小 | 不同类型对象不得别名；多分配单元对象完整计量 |
| `cpp/include/dwpdsim/metrics.hpp`、`cpp/src/bindings.cpp` | 总体 block 计数和 FULL block² compute_cost | 分组件、分请求、分供给来源计数 |
| `src/dwpdsim/input.py`、`cpp/src/bindings.cpp` | 仅有请求时间、request/session id、hash path 的批处理 | 增加稀疏组件事件和有序批量回放 |

因此，需要修改 C++ core、Python 配置/绑定和 benchmark 输入，不能只在现有脚本里换一个 policy 名称，也不能只修改统计公式。

## 3. 两种测试模式

### 3.1 模式 A：固定需求与供给的 backend 回放，优先实现

所有策略读取同一条有序事件流：路径、实际请求的组件对象、上游对象发布。策略只能改变 backend DRAM 驻留、读取和固定规则下的下刷。

- LINEAR miss 不自动生成 checkpoint，也不自动改用较早状态。
- 已有 SSD 副本时，miss 产生读取，可按固定回填规则进入 DRAM。
- 所有层均无副本时，记录 backend 未满足需求；只能等待显式供给，不能凭空插入。
- 首轮 SSD 使用无限容量的组件对象后备存储，固定 dump-all；只保证已经发布且成功下刷的对象可读。
- 可以使用同步、零时间推进的逻辑 I/O 完成来测计数；明确不据此计算排队和延迟。
- 大小按字节计，不需要真实 KV payload、token 内容或 GPU 执行。

这是一种固定工作负载的策略比较。预录的实际 endpoint 需求视为外生输入，不宣称它会在不同引擎缓存状态下保持不变。

### 3.2 模式 B：恢复模型驱动的混合回放，后续实现

输入固定请求路径、原始外生上游事件和 checkpoint 生成规则；本次实际选择的恢复 endpoint 由当前完整组件集合决定。

- 恢复到位置 k，需要该位置的 LINEAR checkpoint 和所需 FULL 区间，两者都可从 DRAM/SSD 取得。
- 在声明的候选范围内选择最深可用或预计成本最低的恢复点，所有策略使用同一规则。
- 记录重放 token/block 数；校准 GPU 模型后才换算计算时间。
- 重算导致的新对象是否生成、何时发布，按同一因果模型计算，作为与 backend 状态有关的派生事件。

模式 B 中，实际 endpoint、读取和派生供给可能随策略变化，不能再要求这些结果逐条相同。也不能继续冻结某次生产运行的全部派生发布，声称已闭环模拟不同策略。

第一轮使用模式 A。模式 B 不作为开发第一个 checkpoint LRU 的前置要求。

## 4. 最小扩展范围与实现顺序

### M1：共享树、分组件状态与字节计量

- 保留现有 block hash/NodeId 和公共 RadixTree；在 Node 中分别保存 FULL、LINEAR 状态。当前每个 Node 对应一个 block hash，segment 通过拓扑聚合，不引入第二棵树。
- 每种组件有对象大小、DRAM 引用、SSD 引用、实际需求统计和存在状态。对象身份至少包含 attention 类型和 hash；首轮配置固定一个模型 namespace。
- FULL、LINEAR DRAM 配额固定；总预算守恒，暂不自动借用配额。
- 第一版大小可以各自固定：一个 FULL block 为 Bf 字节，一个 checkpoint 为 Bl 字节，允许 Bf≠Bl。不同模型/类型间的大小比例先做敏感性分析；单池内部变长对象另作扩展。
- DRAM 直接按对象字节计。SSD 使用共同分配单元 u，大小为 B 的对象分配 ceil(B/u) 个唯一单元；payload 字节与分配字节分别统计。
- 不复制地址分配器；FULL 和 LINEAR 的位置不能因 hash 相同而重叠。checkpoint 完整读写、完整发布，暂不研究部分层重算。
- 默认关闭的旧模式保持原语义和测试；混合事件接口由 `hybrid_attention.enabled=true` 显式开启，不让路径上的每次 miss 都生产 LINEAR 状态。

### M2：显式需求、发布与提交事件

建议的逻辑输入；名称为拟定接口，不是当前可调用 API：

| 事件 | 核心字段 | 作用 |
|---|---|---|
| RequestPath | timestamp_ns、sequence、request_id、affinity_id、hash_ids | 建立需求拓扑，不插入 payload |
| Demand | request_id、attention_kind、FULL 范围或 LINEAR endpoint | 更新实际组件需求，尝试读取 |
| Publish | timestamp_ns、sequence、attention_kind、hash、size_bytes、path_ref | 表示上游实际提供对象 |
| RequestEnd | request_id | 汇总该请求的联合命中/缺失，结束保护范围 |

ReadComplete/StorageWriteComplete 是执行器提交事件，不应为每个策略预先复制同一份完成轨迹。首轮同步提交；异步阶段再增加延迟与保护。

同时间戳使用显式 sequence 定义顺序，尤其是 Demand 早于 Publish 的情况。Publish 若对应输出扩展后的路径，应携带 path_ref 或完整路径声明，不能要求该 hash 一定已在输入路径上。重复发布相同不可变对象不得重复占用，也不计成需求命中。

批处理使用连续数组和对象/路径 offsets，C++ 逐条按序执行；与逐事件调用得到相同结果，不能添加逐 block Python 回调。

### M3：两个策略和一个参考实现

- checkpoint LRU：实际 endpoint 需求刷新；内部点可淘汰；publish 按统一 MRU 插入规则处理。
- checkpoint 收益策略：冷候选预算按字节累计，比较需求收益/字节，平分按 LRU/hash。
- 小规模参考执行器：在 benchmark 中用简单全扫描计算候选，供差分验证。它只用于短 trace，不成为生产热路径或第二套 C++ 权威状态。

所有策略固定 Publish 准入、SSD 回填、Dump/Drop 和写完成语义。对象大于所属池容量时旁路 DRAM；后备存储接收按相同规则处理，不能通过驱逐全部对象仍无法装入的循环卡死。

### M4：指标、benchmark 和回归

新增分组件统计、模式 A 测试、可复现的批量实验脚本。先通过旧模式等价和零假阳性检查，此阶段才达到“可以判断 LINEAR DRAM 淘汰是否有效”的最小交付。

### 后续扩展

按实验暴露的问题加入模式 B、有限 SSD/storage admission、异步 I/O、真实引擎采样和 MQSim。暂不将 GPU 调度、动态分池和完整 SSD 物理模型塞入最小交付。

## 5. 输入准备与上游 selective 规则

已有 hash trace 保留为需求路径来源，不替换其 session 内相对时间与分叉结构。另准备组件需求/发布 sidecar，并记录来源：

1. 生产采样：真实 endpoint、发布位置和时间，可用于固定轨迹模式 A。
2. 因果合成：逐条读取当前及过去路径，按预先声明的 checkpoint 网格、请求结束/发布规则生成 sidecar，标为模型假设。

合成时禁止读取未来 LCP 再提前在分叉处放 checkpoint。请求输出、终止、执行完成时间缺失时，要显式给出简化模型，不能把输入末端默认当作完整会话状态。

`write_through_selective` 不能简单用“hash 查询两次后发布”代替。没有真实插入/发布事件时，使用独立命名的供给过滤模型；可比较全部供给、稀疏供给和该近似过滤模型，但不能宣称精确重现 SGLang。

所有策略共享相同外生事件文件、排序和种子。manifest 记录输入 SHA256、生成规则、block tokens、Bf/Bl、完成时间假设、供给率、首次发布时刻和过滤比例。主实验不能预先给未来才发布的对象放 SSD 副本。手工初态仅用于明确声明的微型测试。

先统计实际 checkpoint 数、复用 endpoint 数、间隔分布和工作集，再设置容量。供给过稀、没有复用或全部能装入 DRAM 时，预期策略差异很小，这是有意义的结果。

## 6. 正确性测试：可观察结果与预期

新增 `tests/test_hybrid_memory.py` 和对应 C++ 集成测试；这些是计划中的文件。用公开回放和诊断结果检查，不依赖 policy 私有函数。

| 编号 | 场景 | 必须观察到的结果 |
|---|---|---|
| T01 | 同一 hash 发布 FULL 与 LINEAR，只触发 LINEAR 池压力 | FULL 数据和 FULL 池占用不被移除，两个 SSD 地址不别名 |
| T02 | 只观察路径或 LINEAR Demand，尚未 Publish | checkpoint 不出现，缺失不触发自动生产或虚假命中 |
| T03 | 三个槽位已有 S2、S4、S8，依次 Demand S2、S8，再发布另一分支 X | 正确 checkpoint LRU 淘汰内部 S4；后续读取 S2/S8 为 DRAM 命中，S4 为 SSD 命中。初始对象均有后备副本 |
| T04 | 反复 Demand S8，路径经过 S2/S4 | S2/S4 的实际需求计数和最近需求时间不被刷新 |
| T05 | checkpoint 仅位于 h8，后续路径在 h4 分叉 | h4 不因树分叉产生 checkpoint；两类型状态仍独立 |
| T06 | 淘汰 DRAM 副本后读取已有 SSD 对象 | 产生正确长度 READ，按固定规则回填，无重复 WRITE，不计全局对象丢失 |
| T07 | 需求发生在 Publish 前，随后在 Publish 后再次需求 | 前次未满足、后次命中；不能跨事件看到未来数据 |
| T08 | checkpoint 大于 LINEAR 配额或需要驱逐多个对象 | 前者 DRAM 旁路，后者占用始终不超预算，无死循环 |
| T09 | 重复 Publish、最后组件删除、仍有子节点 | 无重复容量/写入，节点剪枝同时尊重两组件和拓扑 |
| T10 | 同事件流逐条与分批回放，包含相同时间戳 | 需求结果、占用、READ/WRITE 顺序和汇总相同 |
| T11 | FULL-only 既有样例 | 旧统计与 canonical trace 语义保持一致 |
| T12 | 上游从未提供、backend 曾提供后丢失 | 原因分别计数，不能都归因于淘汰 |
| T13 | 需求热度相同、同大小同 SSD 成本，仅深度不同 | 收益策略不因深度而增加价值；平分使用声明的 LRU/hash 规则 |
| T14 | 旧配置省略开关与显式 enabled=false | 与改造前基线逐事件结果、确定性 stats 字段及 I/O trace 相同；原有策略、迁移与 tick 均覆盖 |
| T15 | 未开启却提交混合事件/非默认混合参数，或开启却缺必要参数 | 在外部边界明确报错，不自动切换，不生成命中或 I/O |
| T16 | 同深度不同分支、相同 hash 的不同组件、无 payload 的 ghost | 不匹配的对象不能满足 Demand；ghost 只更新统计 |
| T17 | 请求 S4，仅存在 S8 或另一分支 S4 | 模式 A 未满足；不能截取/反演 S8，也不能按深度匹配另一分支 |
| T18 | checkpoint 尚在写入/部分完成，或删除后地址已给其他对象 | 当前对象不可命中；过期完成通知不能重新发布旧副本 |
| T19 | LINEAR 命中但必要 FULL 区间有洞，或 FULL 命中而 LINEAR 不存在 | 组件命中分别记录，联合命中为 false；后续成功 Publish 不追溯修改前次结果 |
| T20 | 存储可用性查询成功但 READ 未完成 | 仅可报告 present/in-flight，不能记录已服务命中；读完成后才提交服务结果 |
| T21 | 组件曾分别存在但保护/时序不足以组成同一恢复操作 | 不得据历史 OR 汇总为联合命中 |

异步阶段再测试：未完成写入不可读、I/O 源被保护、全部候选被保护时背压、完成后的容量释放、跨分片 checkpoint 不提前可用。

首轮同步实现对 T18/T20/T21 涉及的在途/部分状态，检查提交边界及对暂不支持的外部事件明确拒绝，不要求先实现排队模型；不得把这些输入直接当成完整可用对象。异步功能加入时，必须执行对应的完整时序测试后才能开启该功能。

模式 B 再测试：LINEAR 存在但 FULL 区间缺失时不能完整恢复；只丢 DRAM 副本和丢最后副本不同；选定的恢复点不超出实际匹配前缀；内部 checkpoint 丢失不使已完整保存的深 endpoint 失效。

短 trace 差分验证不仅比较最后命中率，还比较每次 Demand 结果与每次提交后的组件占用。参考账本只用显式发布/完成/移除事件和精确对象身份维护集合，以独立查表计算期待命中，不调用被测命中函数或复用其驻留布尔值。每个被测命中都要对应账本中的可读副本；联合命中逐项核对必要组件。参考命中也与实际 miss 对照，以发现遗漏供给等功能错误。随机测试固定种子，保留最小失败序列。

旧行为等价采用改造前固定基线结果，覆盖已支持的全部 policy 及有限/无限存储、批处理、配置、Dump/Drop、迁移与后台行为。忽略实际 CPU 耗时、吞吐和输出绝对路径等非确定值，其余公开结果逐项比对，canonical I/O 逐行比对。不能通过更新旧断言、改写 golden 输出或将旧测试切换到混合模式来消除回归。

## 7. 对照算法与消融

模式 A 中 FULL 策略固定为同一个现有策略，LINEAR 比较如下。FULL pool 配额、供给、准入、SSD 策略、回填和 MRU 插入规则全部相同。

| 编号 | LINEAR 候选/热度/排序 | 用途 |
|---|---|---|
| P0 | 直接迁移 FULL 的叶段限制、路径热度和 context 深度排序 | 被迁移算法的对照，不作为正确语义的目标实现 |
| P1 | 正确 endpoint 热度，保留叶段限制和 LRU | 测候选限制的代价 |
| P2 | 内部点可淘汰，路径经过仍刷新，LRU | 测错误刷新带来的保热 |
| P3 | 内部点可淘汰，实际 endpoint 热度，checkpoint LRU | 主要正确基线 |
| P4 | P3 冷候选内，按衰减需求频率排序 | 测频率信息增益 |
| P5 | 相同冷候选内，按预计需求次数 × SSD/DRAM 成本差 / 字节排序 | 测成本与大小信息增益 |

P0→P3 改动多，不能把全部差异归因于优先级公式；P1/P2 与 P3 分别归因。需要单独评价深度排序时，在 P3 相同候选与刷新规则下添加“浅深度优先”变体。

一项重要退化检查：同池对象等大、SSD 成本一致时，P5 与 P4 的分数只差共同系数，应得到相同排序。首轮固定 Bl 时不能宣称验证了变长对象收益；只有引入独立于策略的成本差异或单池变长对象，才能评估成本/字节项的额外价值。

频率使用实际 endpoint 需求而非 Publish 次数。基准与候选策略使用相同的有界历史/ghost 预算；策略额外索引开销单独报告。冷候选 alpha 很小时可能只有一个对象，二级排序退化为 LRU，必须报告候选数。

## 8. 效果实验矩阵

先用可解释的定向 trace 检查机制，再用现有整段会话 trace 的因果 sidecar 检查普遍性，最后才接真实引擎轨迹。

| 负载 | 主要问题 | 不预设的结果 |
|---|---|---|
| 追加会话、多会话交错 | 旧祖先是否被路径刷新错误保热 | endpoint 修正能否减少读取 |
| 多分支共享前缀 | 内部 checkpoint 是否仍被反复实际使用 | 不能默认越浅越该淘汰 |
| 编辑/回退 | 旧 endpoint 的再需求是否存在 | 不能只按最新 endpoint 保留 |
| 热集合混入一次性扫描 | 频率能否抵抗污染 | 新热点适应可能变慢 |
| 热点随时间切换 | 衰减与冷候选是否适应变化 | 复杂策略可能输给 LRU |
| 低复用/稀疏供给 | backend 实际可优化空间 | 允许无明显收益 |
| 两池压力不均 | 固定配额是否成为瓶颈 | 归因为配额，不归因为 victim 排序 |

首轮探索预算：

- LINEAR 容量设为同一训练/准备段测得工作集字节的 5%、20%、50%、100%，并公布实际字节和可容纳对象数；100% 提供无淘汰压力对照。
- 固定 FULL 字节预算；另对两三个 FULL 配额单独做敏感性分析。
- alpha 先在验证段用 0.01、0.1、1.0 比较，主实验冻结一个值；频率衰减窗口也只在训练/验证段选择。
- 固定大小 Bf/Bl 采用模型配置或公开声明的合成比例；先做尺寸比例敏感性，再决定是否实现单池变长。
- 主比较固定全部供给规则，供给稀疏程度作为独立实验轴，不同时重调策略。

不一次做所有参数的笛卡尔积。先完成正确性和 P3/P4/P5 小矩阵，发现差异后再增加归因实验。

真实/大 trace 按时间划分训练、验证、测试，保留会话内顺序与相对间隔；不随机打散请求。测试容量可采用部署预设或由准备段冻结，不用测试未来指导在线策略。预热区间对所有组相同，预热后的缓存状态继续进入测试，计数器单独截取；冷启动实验另报。

随机合成负载至少使用 5 个预先固定种子，策略间逐种子配对比较。固定真实 trace 报告该轨迹的确定性差异；如做置信区间，使用时间段或会话簇重采样，并说明跨会话共享依赖，不能把逐请求独立抽样当成独立样本。

## 9. 评价指标与收益口径

首轮主指标：在相同 DRAM 字节预算下，LINEAR SSD 读取字节和读取请求数相对 P3 的变化；同时报告 P0→P3 的语义修正收益。

```text
read_reduction = (baseline_read_bytes - policy_read_bytes) / baseline_read_bytes
```

baseline 为零时该比值记为不适用，并报告绝对差。读取请求数指逻辑完整对象读取；若展开为多个分配单元，另报单元 I/O 数，避免把一个大 checkpoint 的多次传输当成多次需求。

必须同时报告：

- FULL/LINEAR 各自的 Demand 数、DRAM/SSD 命中、未满足需求、发布对象/字节。
- 未满足需求中：此前从未完成供给、曾供给但当前所有副本丢失、曾供给但副本尚未完成/不可读。第一类再按是否训练/历史已观察到需求区分；不把潜在未来发布算成当前供给。
- 请求级全部必要 FULL 与 LINEAR 数据可满足的比例、全部来自 DRAM 的比例；不能平均两个组件的命中率。
- 逻辑读写字节、分配读写字节、重复写、DRAM/SSD 占用峰值；比较读收益时不得增加未满足需求而不披露。
- 策略 CPU 时间、每次淘汰扫描候选数、元数据/ghost 常驻内存、总进程峰值和回放吞吐。

在固定模式 A、独立资源预算、固定 SSD 和供给下，FULL 需求结果应保持一致；不一致先视为耦合 bug。模式 B 下则允许恢复点变化引起 FULL 读取变化，需要联合归因。

容量公平性分两层：先固定 payload 配额并披露元数据，再在固定总 DRAM（payload 加策略元数据）下复核关键结果。

后续有依据才报告：

- 模式 B：匹配前缀损失、重放 token/block 数；实际校准后报告预计 GPU 时间。
- 异步/设备模型：读取完成与排队；实机部署报告 TTFT/P95/P99。
- MQSim/设备回放：物理写入、WAF、擦除。逻辑写量不是物理耐久改善的直接证明。

不使用现有 `compute_cost = global_misses × context_blocks` 作为 LINEAR 主指标。算法评分和效果评估不能共享未验证的成本公式并据此宣称“预测收益已被证明”；先报告独立 I/O 计数，再用留出的实测成本校准。

## 10. 后续扩展与证据边界

有限 SSD 实验先固定 storage policy 和容量，只改变 LINEAR memory policy，再做 storage admission 消融。模式 B 的上游生成规则、重算和完成反馈必须保持因果。

异步实验才引入 I/O 保护、等待完成和背压。若模式 A 使用同步完成，报告只覆盖无在途重叠的逻辑行为。

MQSim 阶段需扩展 trace 的组件/对象长度表达与 consumer，一并验证 pool-local 地址、拆分 I/O 与依赖。首轮逻辑对象计数不强行写进旧等长 trace 造成长度错误。离线 MQSim 回放只能评估给定 I/O 流，不能自动反向改变 DWPDSim 决策或证明闭环 TTFT。

真实 SGLang 采样应固定引擎版本与上游缓存配置，分别记录路径、选中 endpoint、发布和完成。对比 backend 策略时，若引擎行为受到读取完成反馈影响，应做实机 A/B 或采用明确的闭环模型，不能用一条冻结轨迹覆盖所有因果效应。

## 11. 工程交付与检查

建议新增 benchmark 入口 `benchmark/hybrid_memory_experiment.py`，包含 prepare、run、report 三阶段；报告和输入/输出放 `build/hybrid-memory/`。文件名和子命令为拟定设计，不作为已可运行命令。

每次运行保存源码 SHA 与未提交 diff、新增源码清单/内容摘要、输入和配置 SHA、完整策略参数、种子、初始状态、训练/预热/测试边界、执行时间与汇总。默认聚合输出；仅短正确性测试或显式开启的诊断写逐对象事件，不在大规模热路径逐访问日志。

实现阶段的必要检查：重建 C++ 扩展；Debug CTest；`tests/test_simulator.py`、新混合测试、配置/批处理测试及受影响旧 policy 测试；Ruff；`git diff --check`。修改 canonical trace 或 MQSim consumer 后再运行对应 pipeline 测试和小规模真实 MQSim 回放。

最小交付的完成标准：

1. M1–M4 可通过公开输入复现；开关默认关闭，所有既有功能与改造前基线等价。
2. T01–T21 与短序列独立参考账本一致，组件和联合命中零假阳性，字节/地址/I/O 守恒。
3. 至少完成 P3/P4/P5 在有压力与无压力负载上的对照，公开 P5 退化检查。
4. 能解释收益、无收益或回退的场景；没有预设“新算法必须胜出”的阈值。

最终分别回答结构语义修正是否必要、复杂优先级是否值得、上游供给限制了多少空间，再决定是否投入恢复模型和生产接入。
