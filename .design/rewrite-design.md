# DWPDSim 重写设计

状态：需求基线已确认，待实现。

## 1. 背景与目标

DWPDSim 用于回放 vLLM KVConnector 收到的 KV cache block 请求，模拟 block 在内存、
SLC 和 TLC 中的驻留与淘汰，并输出两类结果：

1. 缓存命中、淘汰、写入量等统计指标；
2. 可继续转换为 MQSim 等 SSD 模拟器输入的通用 READ/WRITE/TRIM trace。

本次为整体重写。现有 Python 原型只作为行为参考，不保留其内部模块边界。新实现不模拟
SSD 内部 GC、擦除和数据搬移，也不在 DWPDSim 内计算设备延迟或写放大。

设计遵循以下原则：

- 核心状态集中在 hash 前缀树，不在多个 manager 中重复维护同一份 block 状态；
- policy 只负责做策略决策，状态变更和 trace 生成由引擎完成；
- 只抽象当前确定需要替换的三类 policy，不建设通用插件框架；
- 明确不做防御性编程：模块默认信任内部契约，不为不应发生的情况增加兜底逻辑；
- 热路径不跨 Python 边界，不重复校验 policy 结果，不复制状态快照，不做事务式回滚；
- 只在配置、Python 输入和输出文件等外部边界做必要校验；
- 测试关注模块协作和端到端行为，不测试私有实现细节。

## 2. 已确认的语义

### 2.1 输入

逻辑输入为：

```text
Request {
    timestamp: uint64,
    hash_ids: sequence<uint64>
}
```

- 一条 Request 对应一次 KVConnector block 请求；
- `hash_ids` 是有序的 block hash 序列，其顺序构成请求前缀；
- 每个 hash 位置计为一次 block access；
- 相同 timestamp 的 Request 按输入顺序处理；
- 同一 Request 内的重复 hash 不去重；
- timestamp 的单位由数据集适配器声明，核心只比较和原样传递该值；
- block 大小默认 8 MiB，整次模拟中固定不变；
- uint64 hash 碰撞不在模拟范围内。

树节点由 `(parent_node_id, hash_id)` 唯一确定，而不是只由 `hash_id` 确定。例如
`[1, 2]` 和 `[3, 2]` 中最后一个 hash 会对应两个不同节点。这一规则直接表达 vLLM
请求之间的共享前缀。

### 2.2 三种访问结果

对请求中的每个 hash，从根节点开始逐级查找或创建树节点，然后按节点驻留状态处理：

| 条件 | 结果 | 行为 |
| --- | --- | --- |
| 节点在内存 | memory hit | 更新统计，不产生 SSD I/O |
| 节点不在内存、在 SLC/TLC | storage hit | 产生 READ；是否进入内存由 MemoryPolicy 决定 |
| 节点在三种介质中都不存在 | global miss | 认为计算已经完成，强制加入内存，不产生 READ |

global miss 后的内存接纳是固定行为，不能被 policy 拒绝。DWPDSim 不模拟重新计算 KV
block 的耗时。

storage hit 无论是否提升到内存，都必须产生 READ。选择 bypass 时，block 在完成本次
访问后仍只驻留在原来的 SLC 或 TLC。

### 2.3 内存淘汰

内存空间不足时，由 MemoryPolicy 选择 victim：

- victim 已有 SLC/TLC 副本：只删除内存副本，不产生 WRITE；
- victim 没有盘上副本：MemoryPolicy 决定 `DROP` 或 `PERSIST`；
- `DROP`：节点变为无驻留状态；
- `PERSIST`：调用 WritePlacementPolicy 选择介质和 stream，随后产生 WRITE。

global miss 新创建的节点不能成为为自身 admission 选择的 victim。

### 2.4 盘上淘汰

SLC 或 TLC 写满时，由独立的 StorageEvictionPolicy 在目标介质上选择 victim：

1. 产生 victim 对应的 TRIM；
2. 释放其逻辑地址；
3. 清除节点上的盘上位置；
4. 使用释放的地址完成当前 WRITE。

如果 victim 仍在内存，它变为 memory-only；否则变为无驻留状态。节点仍保留在 hash
前缀树中，以保留前缀关系和访问统计。“从盘上管理结构中减除”指清除存储驻留和地址
映射，不删除可能仍被后继节点引用的树节点。

同一个节点最多拥有一个盘上副本，即只能在 SLC 或 TLC 之一；允许同时拥有内存副本。
SLC 和 TLC 是两个并列介质，不存在自动的 SLC 到 TLC 分层迁移。

## 3. 实现技术栈

采用 C++ 核心加 Python 接口：

- C++17：前缀树、缓存状态机、三类 policy、地址分配、metrics 和 trace writer；
- pybind11 + CMake + scikit-build-core：构建并暴露 Python 扩展；
- Python：数据集解析、批次组织、运行配置和结果分析。

选择 C++ 的主要原因是本项目需要处理约数亿级 block access，需要精确控制每个树节点
的内存开销，同时方便现有协作者直接修改和调试核心实现。核心不使用逐 block 的 Python
callback；自定义高频 policy 在 C++ 中实现。第一版不提供动态插件系统。

整个模拟按输入顺序单线程执行。全局缓存策略和前缀树状态有顺序依赖，第一版不并行处理
block；文件读取和上层数据预处理可以由 Python 独立并行。

## 4. 模块划分

只保留七个职责明确的模块：

```text
Python API / Dataset Adapter
             |
             v
        SimulationEngine
       /       |        \
  RadixTree  Policies  StorageState
       \       |        /
        MetricsCollector
             |
          TraceWriter
```

### 4.1 Python API

负责将不同数据集转换成连续的 uint64 batch，创建配置并调用 C++ 核心。Python 层不
保存模拟状态，也不参与每个 block 的策略决策。

### 4.2 SimulationEngine

唯一的流程协调者。它逐个处理树节点访问，调用 policy，修改节点状态，并按确定顺序更新
metrics 和 trace。

### 4.3 RadixTree

保存请求前缀关系、节点统计和节点驻留状态，是模拟的核心数据结构。

### 4.4 StorageState

只管理 SLC/TLC 的容量、地址槽和反向地址映射，不包含 chunk、page、GC 或擦除模型。

### 4.5 Policies

包含 MemoryPolicy、WritePlacementPolicy 和 StorageEvictionPolicy 三个 C++ 抽象
接口，以及少量内置实现。

### 4.6 MetricsCollector

在状态转换完成时累加计数。它不反向扫描树或 trace 计算指标。

### 4.7 TraceWriter

使用缓冲顺序写出通用 trace。第一版只有一种规范格式；MQSim 等格式由独立转换脚本
处理。

## 5. Hash 前缀树

### 5.1 逻辑结构

树有一个不对应真实 block 的 root。处理一条请求时：

```text
parent = root
for hash_id in request.hash_ids:
    node = tree.get_or_create(parent, hash_id)
    engine.access(node)
    parent = node
```

每个位置都对应一个有状态的节点，因此不跨 block 边界做路径压缩。这里的 radix 是
uint64 hash 构成的前缀树，而不是对单个 uint64 的 8 个字节再建立 ART。

### 5.2 物理表示

第一版使用扁平表示，避免每个节点一个堆对象：

```text
using NodeId = uint64_t

nodes: std::vector<Node>
child_index: std::unordered_map<EdgeKey, NodeId>
```

`NodeId` 在一次模拟中稳定，可直接被 policy、内存驻留集合和存储地址表引用。具体 hash
表实现由基准测试选择，不在第一版引入自定义 ART 或磁盘索引。

### 5.3 节点字段

```text
Node {
    parent_id: NodeId
    hash_id: uint64

    first_seen_timestamp: uint64
    last_access_timestamp: uint64
    last_hit_timestamp: optional<uint64>
    access_count: uint64

    in_memory: bool
    storage_location: optional<StorageLocation>
}

StorageLocation {
    medium: SLC | TLC
    block_address: uint64
    stream_id: uint32
}
```

`last_hit_timestamp` 在 memory hit 和 storage hit 时更新，global miss 不更新。详细命中
计数放在全局 MetricsCollector 中，不在每个节点重复保存。

不在节点上增加通用 key-value policy metadata。policy 如需 LRU 链表或额外评分，应在
自身结构中按 NodeId 保存，避免动态类型和每节点固定膨胀。

## 6. 缓存状态与处理顺序

节点状态由两个字段直接表达，不再增加一组重复的组合枚举：

```text
in_memory: bool
storage_location: None | SLC location | TLC location
```

一次 block access 的固定顺序为：

```text
1. 在当前 parent 下查找或创建节点
2. 根据节点状态确定 memory hit / storage hit / global miss
3. 执行 READ、admission 和可能的 eviction/write/trim
4. 更新节点访问统计
5. 通知 policy 本次访问结果
6. 更新全局 metrics
7. 进入下一个 hash 节点
```

为了使 trace 可重复，所有由一次 access 派生的 I/O 使用同一 timestamp，并按以下顺序
分配递增的 trace sequence：

- storage hit：先 READ，再执行 promotion 引发的内存淘汰和可能的 WRITE；
- 写入已满介质：先 TRIM，再 WRITE；
- global miss：没有计算 I/O，只有为 admission 引发的可选 WRITE/TRIM。

READ/WRITE/TRIM 一旦写入 trace，DWPDSim 立即应用其逻辑结果，不等待下游 SSD
模拟器返回完成事件。

## 7. Policy 接口

policy 以 C++ 抽象基类表达，接收 NodeId 和只读的必要上下文。它们不直接修改树、地址
表、metrics 或 trace。引擎信任内置 policy 遵守接口约定，不在每次调用前后复制状态
进行防御性校验。

### 7.1 MemoryPolicy

职责：

- storage hit 后是否提升到内存；
- 内存满时选择一个 victim；
- 无盘上副本的 victim 是丢弃还是写盘；
- 维护算法自身的访问和驻留顺序。

最小接口：

```text
admit_storage_hit(context) -> bool
choose_victim(context) -> NodeId
eviction_action(victim, context) -> DROP | PERSIST
on_memory_insert(node_id)
on_memory_access(node_id)
on_memory_remove(node_id)
```

global miss 不调用 `admit_storage_hit`，而是直接 admission。第一版内置 LRU，并允许
配置其 storage-hit admission 和 eviction action。

### 7.2 WritePlacementPolicy

仅在 MemoryPolicy 已决定 `PERSIST` 时调用：

```text
place(node, storage_summary) -> Placement {
    medium: SLC | TLC
    stream_id: uint32
}
```

它负责选择介质和 stream，不负责淘汰盘上 block。内置实现至少包括固定介质/stream，
以及按可配置比例分配 SLC/TLC 的实现。

### 7.3 StorageEvictionPolicy

当 placement 选择的介质没有空闲地址时调用：

```text
choose_victim(medium, incoming_node) -> NodeId
on_storage_read(node_id)
on_storage_write(node_id)
on_storage_remove(node_id)
```

第一版内置每个介质独立的 LRU。policy 必须只返回当前位于目标介质的节点。

## 8. SLC/TLC 与地址管理

配置直接给出两个介质的容量和 stream 数量：

```text
MediumConfig {
    capacity_bytes: uint64
    stream_count: uint32
}
```

容量在启动时换算为固定大小的 block slot。SLC 和 TLC 各自拥有：

- 独立的地址空间；
- `slot -> NodeId` 反向映射；
- 空闲 slot 集合；
- 独立的 StorageEvictionPolicy 状态。

WRITE 从空闲集合取得一个 slot。TRIM 后 slot 立即可重用。stream 只是写入 trace 的
分类信息，不切分物理容量，也不建立 chunk。

如果目标介质已满，只淘汰一个 block 即可完成一次固定大小 block 的写入。第一版不支持
一个 KV block 拆成多个不同地址的 I/O。

## 9. 通用 trace

第一版输出带表头的流式 CSV。定义通用字段，不直接采用某个 SSD 模拟器的格式：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sequence` | uint64 | 全局 I/O 顺序 |
| `timestamp` | uint64 | 原始请求时间 |
| `request_sequence` | uint64 | 全局 block access 顺序 |
| `operation` | enum | READ、WRITE 或 TRIM |
| `medium` | enum | SLC 或 TLC |
| `stream_id` | uint32 | WRITE 必填；READ/TRIM 记录原写入 stream |
| `offset_bytes` | uint64 | 介质内逻辑字节偏移 |
| `length_bytes` | uint64 | 第一版恒为 block size |
| `node_id` | uint64 | 前缀树节点标识 |
| `hash_id` | uint64 | 输入 hash，便于分析 |
| `reason` | enum | STORAGE_HIT、MEMORY_EVICTION 或 STORAGE_EVICTION |

trace 使用缓冲写入，不在内存中累计全部事件。输出 metrics 时同时记录 block size、
timestamp unit、介质容量和 trace schema version，转换脚本据此生成 MQSim 等目标格式。

## 10. Metrics

最终 `metrics.json` 至少包含：

### 输入与命中

- Request 数；
- 总 block access 数；
- memory hit、SLC hit、TLC hit、global miss 数；
- memory hit rate；
- storage hit rate，分母为 memory miss；
- total cache hit rate；
- global miss rate。

### 内存行为

- storage hit 后 promote/bypass 数；
- 内存淘汰总数；
- 已有盘上副本而直接移除的数量；
- DROP 数；
- PERSIST 数；
- 当前和峰值内存驻留 block 数。

### 存储与 trace

- SLC/TLC 各自的 READ、WRITE、TRIM 数和字节数；
- 每个 stream 的 WRITE 数和字节数；
- SLC/TLC 当前和峰值驻留 block 数；
- 同时位于内存和某个 SSD 的 block 数；
- 当前树节点总数。

所有 rate 在生成 snapshot 时由计数计算。DWPDSim 不输出 SSD 物理写入、GC、擦除、
写放大或 DWPD；这些属于下游 SSD 仿真和分析。

## 11. Python 接口

Python 提供易用接口和高吞吐批量接口。

易用接口：

```python
sim.process(timestamp, hash_ids)
```

批量接口使用三个连续 uint64 buffer 表示变长请求：

```python
sim.process_batch(
    timestamps,  # shape: [request_count]
    offsets,     # shape: [request_count + 1]
    hash_ids,    # flattened hashes
)
```

第 `i` 条请求的 hash 范围为 `hash_ids[offsets[i]:offsets[i + 1]]`。大数据集 adapter
应优先使用批量接口；CSV、JSONL、Parquet 等解析逻辑不进入 C++ 核心。pybind11 在
`process_batch` 执行期间释放 GIL，buffer 的生命周期由本次调用覆盖。

建议的顶层配置为：

```text
SimulationConfig {
    block_size_bytes = 8 MiB
    timestamp_unit
    memory_capacity_bytes
    slc: MediumConfig
    tlc: MediumConfig
    trace_path
    progress_interval_requests
}
```

只校验会影响正确执行的配置：容量为正、容量可容纳整数个 block、内存至少能容纳一个
block、stream 数为正。SLC/TLC 容量配比由两个容量自然确定；写入流量配比属于
WritePlacementPolicy 配置。

## 12. 不做防御性编程、错误处理与日志

### 12.1 开发约束

实现中不要做防御性编程。内部模块和内置 policy 按已经定义的接口契约协作，调用方不对
返回值做重复确认，也不为理论上不可达的状态增加 fallback、冗余分支或恢复流程。特别是
不引入以下实现：

- 每次 policy 调用前后的全量状态校验；
- 为失败回滚而复制树、缓存或地址表；
- 对内部非空对象反复做空值检查；
- 捕获无法处理的异常后继续模拟；
- 为所有内部字段和分支增加 assertion；
- policy 返回错误结果后的事务式恢复。

这项约束不取消外部边界校验，也不允许忽略真实的文件 I/O 错误。出现无法继续的错误时
直接终止本次模拟并向 Python 调用方返回错误。

### 12.2 必要错误检查

只在以下边界检查并返回明确错误：

- 配置无法换算成有效 block 容量；
- Python batch 的 dtype、长度或 offsets 不匹配；
- timestamp 相对上一条请求倒退；
- trace 或 metrics 文件无法打开、写入或完成 flush。

开发构建只允许在地址归属和容量计数等核心不变量上保留少量 debug assertion，release
构建不执行。新增 assertion 或内部校验时，应能说明它保护的具体关键不变量。

### 12.3 日志

只记录关键路径：

- INFO：启动配置摘要；
- INFO：按配置间隔输出 request/access 数、处理速度和当前驻留量；
- INFO：结束时输出总耗时、命中率和 trace I/O 数；
- ERROR：输入顺序错误或输出文件 I/O 失败。

不记录逐 block 命中、逐次淘汰、policy 选择或普通状态变化。详细行为通过 trace 和
metrics 分析，不依赖日志。

## 13. 测试范围

测试只覆盖模块间协作和用户可观察的功能，不为简单数据结构、私有方法、每个错误分支或
内部 assertion 编写单元测试。

### 13.1 C++ 模块集成测试

- 两条请求共享前缀时复用相同节点，不同 parent 下相同 hash 产生不同节点；
- memory hit、SLC hit、TLC hit、global miss 四种路径；
- global miss 强制进入内存；
- storage hit 的 promote 和 bypass；
- 内存 victim 已有盘上副本时不重复 WRITE；
- 无盘副本 victim 的 DROP 和 PERSIST；
- placement 正确选择 SLC/TLC 和 stream；
- 目标介质写满后按 `TRIM -> WRITE` 顺序执行；
- 盘上 victim 仍在内存时只清除 storage location；
- metrics 与生成的 trace 数量一致。

### 13.2 Python 功能测试

- 使用 `process` 和 `process_batch` 回放同一小型输入，结果一致；
- 从空状态运行完整场景并生成预期的 `metrics.json` 和 trace；
- 大于一个 batch 的输入保持请求顺序和前缀共享。

测试使用小容量和确定性 policy，通过完整输出断言行为。不会测试旧 Python 原型的 chunk、
GC、transfer 或防御性 rollback 行为。

### 13.3 性能基准

性能基准不作为 pytest 正确性测试。使用现有 SwissAI trace 单独记录：

- block access/s；
- 峰值 RSS；
- 每百万唯一节点的增量内存；
- trace 写出吞吐量。

在拿到实测数据前不预设没有依据的吞吐数字。若唯一前缀节点接近数亿，内存会成为主要
约束；第一版先通过连续的 C++ node vector 和基准数据量化，不提前引入 mmap、分布式
执行或外存树。

## 14. 明确不做的内容

第一版不包含：

- SSD chunk/page/erase/GC 仿真；
- SSD 延迟、队列和完成事件；
- SLC/TLC 自动迁移或层级关系；
- 输入中的显式 read/write/delete 操作；
- 同一节点在 SLC 和 TLC 保存双副本；
- Python 高频 policy callback 或动态插件发现；
- checkpoint、崩溃恢复、分布式或并行状态机；
- 直接输出所有 SSD 模拟器的专用格式；
- uint64 hash 碰撞处理；
- 对旧版 Python 内部 API 的兼容。

## 15. 实现顺序与完成标准

建议按以下顺序完成重写：

1. C++ RadixTree、节点状态和固定访问状态机；
2. 三类 policy 抽象接口及最小内置实现；
3. SLC/TLC 地址管理、通用 trace 和 metrics；
4. pybind11 批量接口和 Python 配置层；
5. 模块集成测试、Python 功能测试和全量性能基准。

第一版完成需满足：

- 已确认的三种访问语义和三类 policy 边界全部实现；
- 同一输入、配置和 policy 产生确定性的 metrics 与 trace；
- trace 中 WRITE 包含正确介质、stream 和地址；
- 盘上淘汰产生 TRIM，但不发生任何内部 GC 行为；
- 大规模输入以 batch 流式处理，输入和 trace 都不整体驻留内存；
- 测试集中在模块协作及端到端输出，没有为防御性校验扩张测试面。
