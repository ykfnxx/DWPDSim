# 混合注意力 backend 回放

当前实现：默认关闭的同步对象回放，以及显式开启的固定 chunked prefill 前缀恢复。FULL/LINEAR 使用同一棵 block hash RadixTree，驻留与统计各自独立；不存放真实 KV payload。旧模式的 API、stats 和 canonical v4 trace 保持原有行为。

## 开启与最小示例

```python
from dwpdsim import (
    DWPDSimulator, HybridAttentionConfig, HybridEvent,
    MemoryConfig, SimulationConfig, StoragePolicyConfig, StorageTierConfig,
)

config = SimulationConfig(
    block_size_bytes=8,  # 混合模式中用作后备存储分配单元
    memory=MemoryConfig(128),
    slc=StorageTierConfig(8, 1),
    tlc=StorageTierConfig(8, 1),
    storage_policy=StoragePolicyConfig(kind="infinite_storage"),
    hybrid_attention=HybridAttentionConfig(
        enabled=True,
        full_block_bytes=8,
        linear_checkpoint_bytes=16,
    ),
)

with DWPDSimulator(config, "build/hybrid-example") as sim:
    sim.process_hybrid(HybridEvent(0, 0, "publish", [11], components="full"))
    sim.process_hybrid(HybridEvent(0, 1, "publish", [11, 22], components="full"))
    sim.process_hybrid(HybridEvent(0, 2, "publish", [11, 22], components="linear"))
    result = sim.process_hybrid(HybridEvent(1, 3, "request", [11, 22], request_id=1))
    assert result["full"] == ["dram", "dram"]
    assert result["linear"] == "dram"
    assert result["all_available"]
    print(sim.hybrid_stats())
```

输出目录需事先存在。开关省略或 `enabled=False` 时，使用原有 `process/run/process_batch`。开启后使用混合事件接口；传错入口明确报错。关闭时设置非默认混合参数也会报错。

支持 `storage_policy.kind="infinite_storage"`，以及有限双 pool 的 `wear_share_round_robin`、`adaptive_endurance`；legacy `memory_policy` 保持默认。默认 shared 分配仅使用总 `memory.capacity_bytes`，FULL/LINEAR 的独立配额必须为 0；两类对象跨类型竞争容量。fixed 和 adaptive 需显式选择。混合模式仅支持下面列出的策略，不静默套用或忽略其他旧策略参数。旧模式仍支持原有全部 policy。

每个 simulator 固定一个模型/格式 namespace，调用方保证 hash 是完整前缀的唯一身份；多模型应隔离 simulator 或先进行一致的 namespace 编码。输入不能重复使用一个 hash 表示不同父前缀，不能截断为容易混淆的局部块 ID。

## 配置

| 字段 | 默认 | 含义 |
|---|---|---|
| enabled | false | 显式开启混合回放 |
| full_capacity_bytes / linear_capacity_bytes | 0 | fixed 时为正的独立 DRAM 配额；shared/adaptive 时保持 0 |
| full_block_bytes / linear_checkpoint_bytes | 0 | 默认对象字节数，开启时必须为正 |
| full_policy | segment_lru | FULL 冷叶段回收；indexed_segment_lru 为增量索引，lru_k 为对象级 LRU-K |
| linear_policy | checkpoint_lru | 支持对象级 lru_k 与下述实验策略 |
| lru_k | 2 | 两类 LRU-K 的 K 值，按实际消费请求记录，发布不算引用 |
| alpha | 0.1 | LINEAR 评分/frontier 搜索的冷候选累计字节比例；fixed 以池配额、adaptive 以当前 LINEAR 软目标为基准 |
| decay_seconds | 60 | 实际需求频率的指数衰减时间 |
| linear_read_cost | 1 | benefit 的统一 checkpoint SSD 读取成本代理 |
| admit_storage_hits | true | SSD 命中后回填 DRAM |
| dump_on_eviction | true | DRAM 淘汰前写入缺少的后备副本；false 则直接移除 DRAM 副本 |
| replay_mode | exact_objects | 精确对象事件；prefix_replay 则恢复前缀并执行简化 chunked prefill |
| tokens_per_block | 0 | 每个输入 hash 对应的 token 数；prefix_replay 必须显式提供正值 |
| checkpoint_interval_tokens | 4096 | 固定 chunk/checkpoint token 间隔；prefix_replay 中必须为 tokens_per_block 的正整数倍 |
| checkpoint_generation | chunked_prefill | request_end 在每个请求最后一个完整 block 处发布一次 LINEAR 状态；不生成中间 chunk 状态 |
| full_drop_fraction / linear_drop_fraction | None | 对没有 SSD 副本的淘汰对象施加确定性的 Drop 配额；范围 [0,1]，None 沿用 dump_on_eviction |
| emit_io_trace | true | false 不输出 hybrid.csv，仍计全部逻辑 I/O |
| recompute_weight | 1 | copy_aware/target_value 的无后备副本回退距离权重 |
| tail_prior | 1 | target 策略给新请求最后一个完整 chunk checkpoint 的初始需求权重 |
| drop_phase | 0 | [0,1) 内的配额初始相位，用于敏感性实验；非零时需配置 Drop 配额 |
| pool_allocation | shared | shared 在总 memory 字节预算内全局选择候选，不设置类型目标；fixed/adaptive 为显式历史实验模式 |
| pool_decay_seconds | 300 | 仅 adaptive 根据当前请求更新需求质量；其他分配模式保持默认 |
| eviction_mode | legacy | retention 根据闲置阈值决定 Dump/Drop；不能与配额或 frontier_guard 混用 |
| full_retention_ns / linear_retention_ns | None | retention 模式中，所选 FULL 段/LINEAR state 闲置严格大于阈值才 Drop；None 表示 Dump |
| linear_retention_policy | idle | frontier 额外允许 Drop 有驻留后继或完整待准入 LINEAR 后继的祖先 checkpoint |

默认 shared 模式满足 `FULL resident_bytes + LINEAR resident_bytes <= memory.capacity_bytes`。两类均为 `lru_k` 时，比较使用统一请求/准入时钟的两个队首，得到全局 LRU-K 次序；对象大小决定释放字节数，不改变排序。FULL 的 `segment_lru/indexed_segment_lru` 配合 LINEAR 的 `checkpoint_lru` 时，在合法 FULL 冷叶段与 LINEAR checkpoint 之间按同一最近访问时钟比较。不支持的跨类型排序组合明确报错。任一类可使用整个总预算，shared 不更新类型软目标。

显式 fixed 模式的两个正字节配额之和不能超过总预算，且不自动互借。显式 adaptive 需 prefix_replay，在共享总上限内按请求需求的衰减字节质量更新软目标；FULL 需求为请求完整 block 的载荷字节，LINEAR 为一个恢复 state 的字节（长度大于 checkpoint 间隔）。首个请求前使用一个 chunk 的模型载荷比例初始化。该模式不等于 shared 的全局候选竞争。

精确对象模式的发布可提供 `size_bytes` 覆盖默认大小，prefix_replay 发布必须符合模型固定大小；同一不可变对象再次发布时大小必须一致。对象大于固定池或共享总容量时直接写入后备存储，旁路 DRAM。

Kimi-K3 默认使用 shared + LRU-K，载荷与回放条件见 [模型配置](kimi-k3.md)。这实现了共享容量的基线，尚未实现 Gandalf 的 Drop/Move 代价估计及准入算法。retention 和 Drop 配额继续由显式参数控制。shared 的 `pool` 统计包含总容量、当前/峰值驻留及 `cross_component_reclaims`，不返回类型目标；adaptive 继续报告软目标与跨池回收。retention 模式包含 action Drop、末副本丢失及回收/写入字节统计。

没有后备副本且设置 `dump_on_eviction=False` 时，移除最后副本后返回 `lost`。当前 benefit 只使用需求频率与读取成本代理，不预测此情况下的引擎重算损失。

`linear_policy` 的原有选择为 `checkpoint_lru/frequency/benefit`。`prefix_replay` 另支持 `prefix_frequency/replay_value/copy_aware/target_frequency/target_value/frontier_lru/frontier_guard`，它们是实验算法。两类对象均可选择 `lru_k`，基线语义见 [Kimi-K3 配置](kimi-k3.md)。所有策略使用相同的恢复可用性判定。

Drop 配额只对没有 SSD 副本的淘汰对象累计；第 n 次此类决策的累计 Drop 数接近 `floor(n*fraction)`，误差不超过一个对象。已有 SSD 的淘汰只移除 DRAM，记为 `backed_evictions`。prefix 统计新增 `eligible_evictions/drop_objects/dump_objects/backed_evictions`，满足 `eligible=drop+dump`、`evictions=eligible+backed`。应分别报告 Drop/eligible、Drop/evictions 和实际写入量，不能混用分母。

## 事件和命中

本节的 `request` 适用于默认 `exact_objects` 模式。`prefix_replay` 使用下一节的独立请求入口；仍可用 `observe/publish` 显式提供初始元数据/对象。

`HybridEvent` 字段：

- `timestamp_ns`：非递减时间；`sequence`：整个事件流严格递增，定义同时间戳先后。
- `action`：`observe`、`publish` 或 `request`。
- `hash_ids`：非空完整前缀路径；沿用一个 hash 一个树节点。
- `components`：`full`、`linear` 或 `both`，默认 both。publish 必须指定 full 或 linear。
- `request_id`：request 事件的唯一 ID；publish/observe 不占用 request ID。
- `restore_blocks`：仅 request 有效，正数，不超过路径长度；None 表示整条路径。完整路径可以包含尚未复用的后缀。
- `size_bytes`：仅 publish 有效，正数；None 使用配置大小。

`observe` 只建立前缀元数据。`publish` 只提供路径最后一个 hash 对应的指定组件；提供 FULL 的末块不意味着前面各块存在。重复发布已可读对象不刷新需求热度，不重复分配或写盘。

`request` 中，FULL 需求是恢复边界前的完整 block 区间，LINEAR 需求是该边界的精确 checkpoint。仅请求 linear 不刷新祖先 checkpoint；checkpoint 淘汰允许选择内部节点。组件类型、前缀或边界不同都不能互相代替。

request 返回各 FULL block 的结果、LINEAR 结果，以及 `all_available/all_dram`。只有 `components="both"` 且所有必需组件可读才令联合 flags 为真，单组件探测不计真正命中。组件结果为 `dram`、`storage`、`never_published` 或 `lost`；未请求 LINEAR 时为 `not_requested`。

整次请求在回填前完成组件判定；同步 READ 完成后才计服务命中。所有必要组件可用才能报告整次请求可满足。读取完成后才执行回填和潜在淘汰，不能利用回填把此前缺失变成命中。未满足需求不会自动生成对象。

当前不接受 pending、部分 shard、异步完成或改变恢复点的事件。未支持的事件明确拒绝。需求和供给时间必须来自采样或公开的生成模型；不能把树分叉或未来复用位置当成已生成 checkpoint。

## 固定 chunked prefill：默认每 4096 token 一个 checkpoint

也可设置 `checkpoint_generation="request_end"`：每个有完整 block 的请求只在
`floor(input_tokens/tokens_per_block)*tokens_per_block` 位置发布一次 LINEAR 状态。
当前输入只携带完整 block hash，因此不保存无法验证身份的部分 block 尾部状态；
不足一个完整 block 的请求不生成状态。重复发布同一已可读端点不分配新副本、
不刷新 LRU、不增加实际需求计数。即使端点就是本次恢复源，也记录一次发布机会。
`generated_checkpoint_tokens` 在此模式下为空或仅含这个端点，`checkpoint_interval_tokens`
不控制其生成位置。FULL+LINEAR 联合恢复条件及 N−1 上限保持相同。
仅实际消费的 LINEAR 恢复状态刷新 LRU 和 demand；路径扫描及评分估计不刷新祖先状态。
新对象准入只建立初始 LRU 位置，不计为 demand。request_end 的配比需求门槛及回退距离
归一化单位为一个完整 block，chunked_prefill 则保持原来的 checkpoint 间隔。

`prefix_replay` 是 backend 隔离的同步生成模型：从最深完整可读的 checkpoint 恢复，逻辑执行后缀，在固定绝对 token 边界发布 checkpoint。每个完整 chunk 的间隔由 `checkpoint_interval_tokens` 控制，默认 4096；不足一个 chunk 的尾部不额外保存末端状态。这个模型不运行真实神经网络。

下面使用与上一示例相同的合成对象大小，扩大共享 DRAM 总容量并开启前缀模式：

```python
from dataclasses import replace
from dwpdsim import HybridPrefillRequest

prefill_config = replace(
    config,
    memory=MemoryConfig(1088),
    hybrid_attention=replace(
        config.hybrid_attention,
        replay_mode="prefix_replay",
        tokens_per_block=128,
        checkpoint_interval_tokens=4096,
    ),
)
hashes = list(range(1, 79))  # 78 个完整 block，9984 token
with DWPDSimulator(prefill_config, "build/hybrid-prefill-example") as sim:
    first = sim.process_hybrid_prefill(HybridPrefillRequest(
        0, 0, 0, hashes, input_tokens=10000,
    ))
    assert first["generated_checkpoint_tokens"] == [4096, 8192]
    second = sim.process_hybrid_prefill(HybridPrefillRequest(
        1, 1, 1, hashes, input_tokens=10000,
    ))
    assert second["restore_tokens"] == 8192
    assert second["prefill_compute_tokens"] == 1808
    assert second["generated_checkpoint_tokens"] == []
```

`HybridPrefillRequest` 包含 `timestamp_ns/sequence/request_id/hash_ids`，时间和唯一性规则与对象模式一致。可选 `input_tokens` 默认取 `len(hash_ids)*tokens_per_block`；显式指定时必须恰好提供所有完整 block 的 hash，不足一 block 的尾部没有 hash。短于一个 block 的非空请求可用空 `hash_ids` 和显式 token 数表示。

恢复上限固定不超过 `input_tokens-1`，可选 `max_prefix_tokens` 可进一步限制，包括设为 0。长度正好为 8192 的重复请求，因此最多从 4096 恢复，重新执行最后一个 chunk；不会使用 `S8192` 宣称零计算。不能使用分叉之后、另一分支、缺少所需 FULL 区间或最后副本已经丢失的 checkpoint。

恢复判定发生在当前请求创建/发布新数据之前。只消费所选 checkpoint 和 FULL `[0,r)`，候选扫描不刷新其他 checkpoint 的热度；所有源先读取完成，再进行回填。随后逻辑执行 `[r,N)`：逐完整 block 发布 FULL，在经过 `4096/8192/...` 等配置边界时发布 LINEAR。FULL 可以保存到最后一个完整 hash block，不受稀疏 checkpoint 终点裁剪。显式初始 checkpoint 若不在固定网格上，后续仍在下一绝对网格边界开始保存。

该简化模型的生成与 backend 完整接收同步完成，没有上游 GPU/Host 缓存、selective 插入计数过滤、分叉跟踪特例、decode 或传输延迟。每个新 checkpoint 直接供给 backend，不代表 SGLang 的真实发布比例；当前模拟边界见 [Kimi-K3 配置](kimi-k3.md)。同一不可变 hash 的重新计算可再次产生相同 checkpoint；已可读副本不重复分配，也不因重复发布刷新热度。

结果返回实际 `restore_tokens/restore_hash_id`、FULL 与 LINEAR 的来源、`generated_checkpoint_tokens`，以及：

```text
L = 先前已发布 FULL 路径的最长公共前缀（含已丢失副本的历史）
prefill_compute_tokens = N-r
replayed_prefix_tokens = L-r
new_suffix_tokens = N-L
```

只有 block hash 时，`L` 精确到完整 block，无法识别部分 block 内的 token 相等性。观察过的 ghost 不算已执行前缀。`r=0` 返回 `linear="initial"`，不计缓存恢复。缺失 FULL、对齐和末 token 上限都可能贡献重算，不能全部归因于 LINEAR policy。

新模式的统计采用 `schema_version=2`：保留 `requests/full/linear/io_events/tree_nodes`，增加 `prefill` 汇总；不返回精确对象模式的 `joint/all_available` 请求指标，避免将部分前缀恢复误解为整条输入命中。`prefill` 包含恢复请求数、全部来源为 DRAM 的恢复数、输入/匹配/恢复/计算/重算 token 数和生成 checkpoint 次数。生成次数包含重新执行得到的相同 endpoint，不能当作唯一 checkpoint 数。

批量入口：

- `process_hybrid_prefill_requests(requests)`：C++ 内依次处理请求并返回结果。
- `run_hybrid_prefill(requests, batch_requests=1024)`：有界流式处理，只返回汇总。
- `process_hybrid_prefill_batch(timestamps_ns, sequences, request_ids, input_tokens, max_prefix_tokens, offsets, hash_ids)`：连续一维 uint64 列；`input_tokens=0` 表示按完整 blocks 推断，`max_prefix_tokens=UINT64_MAX` 表示默认 `N-1`，0 表示禁止复用。offsets 允许相邻相等，以表示没有完整 hash block 的短请求。

所有新入口与逐请求行为一致，供给在各次运行中依据恢复位置派生，不预先冻结。默认 `exact_objects` 拒绝新请求入口；其旧 API、stats 和事件语义不变。

生成模型固定使用配置中的 FULL/LINEAR 对象大小；显式 `publish` 初始化同一路径时，大小也必须一致。变长对象实验继续使用精确对象模式。

## 批量输入与统计

- `process_hybrid_events(events)`：一次 C++ 调用回放一批事件，返回其中 request 的结果。
- `run_hybrid(events, batch_events=1024)`：有界流式处理，返回汇总，不保存全部 request 结果。
- `process_hybrid_batch(timestamps_ns, sequences, actions, components, request_ids, restore_blocks, sizes, offsets, hash_ids)`：连续一维 uint64 列；actions 0/1/2 对应 observe/publish/request，components 0/1/2 对应 full/linear/both。边界与大小为 0 时使用默认值；offsets 长度为事件数加一，每个事件至少有一个 hash。

批量和逐事件共享执行器；不重新排列事件。外部非法事件会报错，已成功执行的前序事件不回滚。

`hybrid_stats()` 提供独立的组件指标；开启后的 facade `stats()` 返回 `{"hybrid_attention": ...}`，关闭时旧 stats 结构保持不变。`hybrid_inspect(hash_id, component)` 只读查询发布历史、驻留、地址和需求计数，不改变命中或热度。

组件指标区分对象需求/命中、首次未供给缺失与最后副本丢失、发布次数/字节、驻留峰值、逻辑及分配 I/O 字节、淘汰/旁路和候选扫描数。publications 包含重复发布事件，不能用它代替唯一对象数或需求次数。joint 指标只统计 components=both 的请求。

C++ Node/RadixTree 保留值复制语义，混合状态复制独立；const 树视图只能读取组件状态。关闭时不分配组件状态。

元数据留在公共需求树中以保持前缀身份和需求历史；首版没有 ghost 容量上限或自动剪枝。LRU-K 的驻留顺序由 C++ 有序索引维护；其他候选策略的开销应分别测量。大规模 trace 应同时观察树节点数和进程内存，不能只按 payload 配额比较总内存收益。

默认混合 I/O 输出为 `trace_path + ".hybrid.csv"`。无限后备模式保持原有 8 列 READ/WRITE 对象格式；关闭 trace 时不创建文件或地址列表。

有限双 pool 复用已有 RR/adaptive_endurance policy 实例、RadixTree 段、回收和后台调度。同一 prefix node 的 FULL/LINEAR SSD 副本作为 placement bundle 共用 tier/stream，组件的 payload、DRAM 驻留、独立 SSD 地址与可恢复性分别维护。容量按实际分配单元计，整段 TRIM 同时清除其中的 SSD 组件；DRAM 副本不受影响。容量不足可拒绝 Dump，并记 admission_rejections。有限模式即使关闭 trace，也保留地址以便回收复用。不同组件的自由独立 tier placement 不属于本版 baseline。

有限模式的对象 trace 增加 5 列：`storage_tier,stream_id,reason,move_id,depends_on_sequence`，支持 READ/WRITE/TRIM 及迁移 READ -> WRITE -> TRIM 完成依赖。消费读取全部完成后才统一刷新段 recency、迁移及回填；访问迁移复用已消费的 READ。`HybridPrefillRequest.affinity_id` 用于 adaptive session gap；0 沿用 request_id 回退语义。现有 columnar 批接口没有 affinity 列，使用默认 0；需要保留 session 身份时使用对象请求接口，WEKA runner 已传入稳定的 root session ID。

`benchmark/hybrid_device.py` 使用 `dwpdsim.hybrid_mqsim` 流式生成 `DWPDSIM_DEPENDENCY_V1`。按整个 allocated unit 搬运（包含 padding），保留 pool-global 的地址复用/读写依赖，并将大对象拆为合法命令。设备端数据缓存关闭，初始占用为零，当前 baseline 使用 ideal mapping 来隔离映射缓存干扰。旧 canonical v4 converter 保持独立。

实验设备配置由脚本生成：6 channel，SLC 0/1，TLC 2/3/4/5，每 channel 2 chip、每 chip 2 die、每 die 4 plane，每 block 4224 page，每 page 16KiB；1/4TB 两档分别取173/689 block/plane，OP0.125。实际逻辑容量按1MiB单元保持SLC:TLC精确1:2；policy logical_fill_fraction=0.98与设备OP分别报告。SLC/TLC P/E3/7及全部时序使用用户给定值。

4TB设备的主机GMT元数据超过本机可用RAM，因此可使用隔离的MQSim host-backing构建。`benchmark/build_mqsim_host_backing.py`从固定MQSim提交导出源码并应用 `mqsim-host-backing.patch`；只把模拟器主机端的GMT内存放到磁盘映射，保留原有entries、FTL和模拟时间，不修改原MQSim工作区。运行时 `MQSIM_GMT_DIR` 必须位于磁盘文件系统，不能使用tmpfs。须先核对普通与映射版本的相同trace计数结果。

`example/run_pipeline.py` 支持 `.env.example` 中的 `DWPDSIM_HYBRID_*` 配置；默认使用 HybridEvent JSONL。设置 `DWPDSIM_HYBRID_REPLAY_MODE=prefix_replay` 和正的 `DWPDSIM_HYBRID_TOKENS_PER_BLOCK` 后，输入改为 HybridPrefillRequest JSONL；`DWPDSIM_HYBRID_CHECKPOINT_INTERVAL_TOKENS` 默认 4096。无限后备模式不运行 MQSim。

## 验证与实验

```bash
.venv/bin/python -m pytest tests/test_hybrid_shared.py tests/test_hybrid_memory.py tests/test_hybrid_prefill.py tests/test_kimi_cache.py
.venv/bin/python benchmark/kimi_no_trace.py --requests 10000 --rounds 3
```

解释器需先按仓库说明重建扩展。测试覆盖 FULL/LINEAR 联合恢复、内部 FULL 缺口、独立对象
字节预算、LRU-K 历史及批量/聚合等价；基准采用固定真实 Kimi-K3 载荷的多会话请求，
对象发布由每次实际恢复与计算派生。`kimi_no_trace.py` 显式保留历史 fixed 分区，用于复现既有
性能对照；共享 DRAM 的 WEKA 运行方法见 [更正报告](../report/shared-dram-correction.md)。
配置、验证范围和性能口径见 [Kimi-K3 指南](kimi-k3.md)。

同步逻辑回放不测实际 TTFT、排队、写放大或物理耐久。有限 SSD 的逻辑 I/O 可交给 MQSim，
异步保护和完整引擎反馈属于后续扩展。`prefill.joint_token_hit_rate` 只计 FULL/LINEAR 交集恢复的 token。
