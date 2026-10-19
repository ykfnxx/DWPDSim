# 混合注意力 backend 回放

当前实现：默认关闭的同步对象回放。FULL/LINEAR 使用同一棵 block hash RadixTree，驻留与统计各自独立；不存放真实 KV payload。旧模式的 API、stats 和 canonical v4 trace 保持原有行为。

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
        full_capacity_bytes=64,
        linear_capacity_bytes=64,
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

首版要求 `storage_policy.kind="infinite_storage"` 且 legacy `memory_policy` 保持默认；两池容量为正、之和不超过总 memory 配额。混合模式仅支持下面列出的策略，不静默套用或忽略其他旧策略参数。旧模式仍支持原有全部 policy。

每个 simulator 固定一个模型/格式 namespace，调用方保证 hash 是完整前缀的唯一身份；多模型应隔离 simulator 或先进行一致的 namespace 编码。输入不能重复使用一个 hash 表示不同父前缀，不能截断为容易混淆的局部块 ID。

## 配置

| 字段 | 默认 | 含义 |
|---|---|---|
| enabled | false | 显式开启混合回放 |
| full_capacity_bytes / linear_capacity_bytes | 0 | 独立 DRAM 配额，开启时必须为正 |
| full_block_bytes / linear_checkpoint_bytes | 0 | 默认对象字节数，开启时必须为正 |
| full_policy | segment_lru | FULL 组件的冷叶段选择与整段尾部回收 |
| linear_policy | checkpoint_lru | checkpoint_lru、frequency 或 benefit |
| alpha | 0.1 | 频率/收益策略冷候选累计字节预算比例 |
| decay_seconds | 60 | 实际需求频率的指数衰减时间 |
| linear_read_cost | 1 | benefit 的统一 checkpoint SSD 读取成本代理 |
| admit_storage_hits | true | SSD 命中后回填 DRAM |
| dump_on_eviction | true | DRAM 淘汰前写入缺少的后备副本；false 则直接移除 DRAM 副本 |

FULL 和 LINEAR 的字节配额不自动互借。发布可提供 `size_bytes` 覆盖默认大小，从而研究同池变长对象；同一不可变对象再次发布时大小必须一致。对象大于对应池容量时直接写入后备存储，旁路 DRAM。

没有后备副本且设置 `dump_on_eviction=False` 时，移除最后副本后返回 `lost`。当前 benefit 只使用需求频率与读取成本代理，不预测此情况下的引擎重算损失。

## 事件和命中

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

request 返回各 FULL block 的结果、LINEAR 结果，以及 `all_available/all_dram`。组件结果为 `dram`、`storage`、`never_published` 或 `lost`；未请求 LINEAR 时为 `not_requested`。

整次请求在回填前完成组件判定；同步 READ 完成后才计服务命中。所有必要组件可用才能报告整次请求可满足。读取完成后才执行回填和潜在淘汰，不能利用回填把此前缺失变成命中。未满足需求不会自动生成对象。

当前不接受 pending、部分 shard、异步完成或改变恢复点的事件。未支持的事件明确拒绝。需求和供给时间必须来自采样或公开的生成模型；不能把树分叉或未来复用位置当成已生成 checkpoint。

## 批量输入与统计

- `process_hybrid_events(events)`：一次 C++ 调用回放一批事件，返回其中 request 的结果。
- `run_hybrid(events, batch_events=1024)`：有界流式处理，返回汇总，不保存全部 request 结果。
- `process_hybrid_batch(timestamps_ns, sequences, actions, components, request_ids, restore_blocks, sizes, offsets, hash_ids)`：连续一维 uint64 列；actions 0/1/2 对应 observe/publish/request，components 0/1/2 对应 full/linear/both。边界与大小为 0 时使用默认值；offsets 长度为事件数加一，每个事件至少有一个 hash。

批量和逐事件共享执行器；不重新排列事件。外部非法事件会报错，已成功执行的前序事件不回滚。

`hybrid_stats()` 提供独立的组件指标；开启后的 facade `stats()` 返回 `{"hybrid_attention": ...}`，关闭时旧 stats 结构保持不变。`hybrid_inspect(hash_id, component)` 只读查询发布历史、驻留、地址和需求计数，不改变命中或热度。

组件指标区分对象需求/命中、首次未供给缺失与最后副本丢失、发布次数/字节、驻留峰值、逻辑及分配 I/O 字节、淘汰/旁路和候选扫描数。publications 包含重复发布事件，不能用它代替唯一对象数或需求次数。joint 指标只统计 components=both 的请求。

C++ Node/RadixTree 保留值复制语义，混合状态复制独立；const 树视图只能读取组件状态。关闭时不分配组件状态。

元数据留在公共需求树中以保持前缀身份和需求历史；首版没有 ghost 容量上限或自动剪枝。FULL 选段在 C++ 中扫描当前驻留集合和祖先，尚未复用旧 indexed 策略的全部增量优化。大规模 trace 应同时观察树节点数和进程内存，不能只按 payload 配额比较总内存收益。

混合 I/O 输出为 `trace_path + ".hybrid.csv"`，每行对应一次完整对象 READ/WRITE，包含组件、hash、payload/allocated 字节和分号分隔的存储地址。两类对象共享既有 TLC 分配器，但地址各自独立。该文件是独立逻辑格式，不能传给现有 MQSim canonical v4 consumer。

`example/run_pipeline.py` 支持 `.env.example` 中的 `DWPDSIM_HYBRID_*` 配置；开启时请求文件为上述字段的 HybridEvent JSONL。无限后备模式不运行 MQSim。

## 验证与实验

```bash
build/test-venv/bin/python -m pytest tests/test_hybrid_memory.py -q
build/test-venv/bin/python benchmark/hybrid_memory_experiment.py prepare
build/test-venv/bin/python benchmark/hybrid_memory_experiment.py run
build/test-venv/bin/python benchmark/hybrid_memory_experiment.py report
```

解释器需先按仓库说明重建扩展。benchmark 是因果生成的固定 backend 需求/供给实验，默认 5 个种子；事件文件 SHA、配置来源和实现 SHA/未提交源码摘要写在 build 输出下。它不精确模拟 SGLang selective 发布或 GPU 生命周期。

当前测试覆盖独立 LRU/全扫描账本、内部 checkpoint 淘汰、FULL 区间缺口、跨分支前缀检查、不同组件/对象尺寸、丢失后重新发布、批处理顺序和 default-off 回归。同尺寸同读取成本时，benefit 与 frequency 必须退化为相同排序。

同步逻辑回放不测实际 TTFT、排队、写放大或物理耐久。有限 SSD 策略、异步保护、checkpoint 恢复点选择和引擎反馈仍属于后续扩展。算法候选的完整设计与测试计划分别见 [算法方案](hybrid-attention-policy-design.md) 和 [测试方案](hybrid-attention-test-plan.md)。
