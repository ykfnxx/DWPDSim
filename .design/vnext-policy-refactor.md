# DWPDSim vNext Policy 重构设计

状态：已实现。

本文是 DWPDSim vNext 的唯一设计文档。vNext 采用破坏性接口，不保留现有 policy
接口、实现和配置兼容层。现有行为中仍有实验价值的策略将在新接口上重新实现为 baseline。

`KVCache_Radix_MQSim_DWPD_online_v7` 只作为算法参考。DWPDSim 不保留该参考实现的独立
模拟器、私有 Radix、segment 容器、LBA allocator、事件回放协议或运行入口。

## 1. 重构目标

vNext 支持三类相互独立的输入事件：

```text
Request / Access Event
Memory Dump Event
Background Tick Event
```

其中：

- Request/Access 更新共享 Radix 上的访问与驻留状态；
- Memory Dump 是内存管理向存储管理提交数据的唯一入口；
- Background Tick 独立于 Dump 驱动后台空闲淘汰和老化迁移；
- 前台 Dump 容量回收和后台维护都由同一个 storage policy 决策；
- Simulator 是唯一状态修改者和 I/O 执行者；
- MQSim 继续消费 DWPDSim 输出的物理 I/O trace，不参与 policy 决策。

旧 `MemoryPolicyBase`、`WritePlacementPolicyBase`、`StorageEvictionPolicyBase` 及其 Python
配置接口全部删除。

## 2. 系统所有权

### 2.1 DWPDSim Core

DWPDSim Core 唯一持有以下权威状态：

- 一棵全局 `RadixTree`；
- 节点的内存与存储驻留状态；
- SLC/TLC 容量、地址分配和释放；
- request、timer 和 I/O 的逻辑时间顺序；
- trace sequence、metrics 和错误状态。

Memory、SLC 和 TLC 是同一棵 RadixTree 上的不同驻留集合。任何 policy 都不得维护另一棵
Radix、另一份 parent/children 拓扑、另一份节点驻留表或另一套 LBA allocator。

### 2.2 Policy

Policy 只读取 Core 提供的只读视图并返回决策。Policy 可以维护算法所需的派生状态：

- memory/storage LRU 索引；
- segment 首次写入时间和最后逻辑访问时间；
- request/session gap estimator；
- SLC/TLC 累计 program bytes；
- 每个 stream 的 program bytes；
- placement round-robin cursor；
- 定时维护候选索引。

这些派生状态必须通过 Simulator 的 commit 通知更新。规划失败、被拒绝的 Dump 或尚未执行
的迁移不能提前计入 program bytes、live bytes 或 metrics。

### 2.3 Simulator

Simulator 负责：

1. 推进虚拟时间和后台 tick；
2. 构造 request、access、Dump 和 capacity pressure context；
3. 调用 policy 获取决策；
4. 校验执行所需的外部边界条件；
5. 修改 RadixTree 和 StorageState；
6. 输出 READ/WRITE/TRIM；
7. 更新 metrics；
8. 向 policy 发送已提交的状态变化。

## 3. 新 Policy 模型

vNext 只保留两类顶层 policy：

```cpp
class MemoryPolicy;
class StoragePolicy;
```

### 3.1 MemoryPolicy

MemoryPolicy 负责：

- storage hit 后是否进入内存；
- 内存空间不足时选择最久未访问的 memory leaf segment；
- 决定对该 leaf segment 执行 leaf-only `Drop`，还是执行向 parent 贪婪的 `Dump`；
- 维护内存侧候选顺序和派生状态。

核心决策为：

```cpp
enum class MemoryEvictionAction {
    Drop,
    Dump,
};

struct MemoryEvictionDecision {
    NodeId leaf_segment_endpoint;
    MemoryEvictionAction action;
};
```

memory leaf segment 与 Memory 有交集，且不存在仍与 Memory 相交的后代 segment。一次决策
指定起始 leaf segment 和执行模式。`Drop` 只释放该 leaf segment 的内存驻留并停止；
`Dump` 以完整 segment 为单位向 parent segment 贪婪回收，已经全部写盘的 segment 只删除
内存副本并继续向上，遇到第一个含未写盘 block 的 segment 时写盘并停止。已有 storage copy
的 block 不重复写入。action 是 MemoryPolicy 的逐次决策，不是 `SimulationConfig` 中的全局
开关；当前 `baseline_lru` 固定返回 `Dump`。

### 3.2 StoragePolicy

StoragePolicy 是一个完整的存储管理算法，而不是 placement、eviction 和 timer policy 的
松散组合。它统一负责：

- Dump placement：选择 SLC/TLC 和 stream；
- Dump 前台容量回收；
- 后台 idle eviction；
- SLC 到 TLC 的老化迁移；
- storage logical access 对 LRU 和迁移状态的影响；
- wear/program-byte 状态；
- request gap 与动态阈值。

接口为：

```cpp
class StoragePolicy {
  public:
    virtual ~StoragePolicy() = default;

    virtual BackgroundSchedule background_schedule() const = 0;

    virtual void on_request_begin(
        const RequestContext& request,
        const StorageView& storage
    ) = 0;

    virtual DumpPlacementDecision place_dump(
        const DumpContext& dump,
        const StorageView& storage
    ) = 0;

    virtual std::optional<CapacityAction> reclaim_for(
        const CapacityPressureContext& pressure,
        const StorageView& storage
    ) = 0;

    virtual std::optional<MaintenanceAction> next_background_action(
        const BackgroundTickContext& tick,
        const StorageView& storage
    ) = 0;

    virtual std::optional<MaintenanceAction> on_storage_access(
        const StorageAccessContext& access,
        const StorageView& storage
    ) = 0;

    virtual void on_commit(
        const StorageMutation& mutation,
        const StorageView& storage_after_commit
    ) = 0;
};
```

`reclaim_for()` 和 `next_background_action()` 每次最多返回一个动作。Simulator 执行动作并
提交状态后再次调用 policy，使下一次决策能看到最新容量和 Radix 驻留状态。

## 4. Request 与时间接口

### 4.1 RequestContext

request context 包含：

```cpp
struct RequestContext {
    TimestampNs timestamp_ns;
    RequestId request_id;
    AffinityId affinity_id;
    std::span<const HashId> ordered_hashes;
    std::span<const NodeId> protected_prefix;
};
```

约束如下：

- `ordered_hashes` 保留完整顺序和重复访问；
- `request_id` 在一条 trace 内唯一；
- `affinity_id` 表示稳定的 session/request-stream 身份；
- adaptive-endurance policy 每个外部请求只观察一次 session gap；
- adaptive-endurance 输入适配器应提供稳定的非零 affinity；
- affinity 为零时退化到 request/segment identity，但这种输入无法形成准确的
  跨请求 session gap，必须计入配置和结果元数据。

### 4.2 时间单位

adaptive-endurance policy 使用秒级 idle 和 promotion 参数，因此时间单位不能继续只是说明字符串。
Python dataset adapter 必须将输入时间转换为统一的 `TimestampNs`，C++ Core 只处理纳秒。

所有 request 和 batch 的 timestamp 非递减。相同 timestamp 按以下固定优先级处理：

```text
Background Tick
Request Begin
Access
Memory Dump / Foreground Reclaim
Request End
```

同一事件内使用单调递增 sequence 保证 trace 可重复。

## 5. Radix Segment 与只读视图

### 5.1 唯一 Radix

全系统只有 DWPDSim 的公共 RadixTree。所有 policy 必须通过只读查询使用该树，不得复制
segment、children 或 online Radix 状态。

### 5.2 SegmentView

Segment 是从当前全局 Radix 拓扑派生的非持久只读视图：

```cpp
struct SegmentView {
    NodeId segment_top;
    NodeId segment_endpoint;

    // 固定为 top -> endpoint，不能依赖内部 scratch 的反向顺序。
    std::span<const NodeId> ordered_nodes;
};
```

Policy 不得跨调用保存 `SegmentView`、`Node&` 或 `Node*`。需要长期索引时只保存稳定 NodeId，
每次决策重新从 StorageView 解析最新拓扑。

### 5.3 Storage leaf

后台和容量淘汰使用 storage-residency-aware leaf。它不能简单等同于全局
`RadixTree::is_leaf()`：一个 segment 可以拥有仅在内存中的后代，同时仍是盘上可淘汰 leaf。

Core 提供只读的 storage topology 查询，能够判断：

- segment 是否与目标 tier 相交；
- segment 是否存在任何 storage-resident child segment；
- segment 是否与当前 protected set 相交；
- segment 在指定 tier 上的实际 block 数和 bytes。

Core 仍然拥有拓扑和驻留真值；Policy 的 leaf LRU 只是候选索引，使用前必须通过只读视图
解析当前 segment。

## 6. Memory 回收边界

### 6.1 Dump 向上贪婪，Drop 只剪枝 leaf

MemoryPolicy 按 segment LRU 选择 memory leaf segment，并返回本次 action：

```text
Select oldest memory leaf segment
        |
        +-- Drop --> remove the selected leaf segment --> stop
        |
        +-- Dump --> segment has unwritten memory block?
                         | no                         | yes
                         v                            v
                  remove segment and          dump missing blocks,
                  move to parent              remove segment, stop
```

`Drop` 不调用 `parent(segment_top)`；删除内存驻留后，只运行公共 `prune_segment()`，因此只有
同时满足无内存、无 storage copy、无子节点的空拓扑会被递归剪掉。

`Dump` 的每一步都以 segment 为单位，并使用 `parent(segment_top)` 定位 parent segment：

- `memory_nodes = segment ∩ Memory`；
- `write_nodes = {node ∈ memory_nodes | !node.on_storage}`；
- `write_nodes` 为空时，整段释放后继续 parent segment；
- `write_nodes` 非空时，对整段执行一次 Dump 并停止；
- 到达根边界时结束，本轮可以不产生 WRITE。

### 6.2 Dump 是 storage policy 的写入输入

Global miss 只把新 block 放入内存，不直接调用 storage placement。只有 MemoryPolicy 在
内存淘汰时返回 `Dump`，才构造 storage policy 输入。

```cpp
struct DumpContext {
    RequestContext request;
    SegmentView segment;

    // segment 中本次没有 storage copy、确实需要写入的节点。
    std::span<const NodeId> write_nodes;

    std::uint64_t write_blocks;
    std::uint64_t write_bytes;
    std::span<const NodeId> protected_nodes;
};
```

已有 SLC/TLC copy 的节点不进入 `write_nodes`，也不产生重复 WRITE。

### 6.3 Dump placement

StoragePolicy 对完整 DumpContext 做 placement。每个 policy 对一个 Dump segment 只做
一次 tier/stream 决策，`write_nodes` 共享该 placement。baseline policy 在新接口上重实现
自身的 placement 规则；新接口不复用旧 per-node placement ABI。

Simulator 在写入前以整个 `write_bytes` 检查目标 tier 容量。Dump 不能发生部分写入：

```text
Place complete Dump
        |
        v
Enough capacity? -- yes --> Commit all writes
        |
        no
        v
Select and commit one capacity victim
        |
        +--------------------------> Recheck capacity
```

如果没有合法 victim，Dump 返回明确的 `NoSpace`/`AdmissionRejected`。为此次尝试已经提交的
容量淘汰保留，但本次 Dump 不产生任何 WRITE。随后 MemoryPolicy 选中的节点仍需离开内存，
没有其他副本的节点成为 drop，并记录独立的 rejection/drop 指标。

### 6.4 Protected nodes

当前请求正在使用的已命中前缀在 request scope 内受到保护。前台 capacity reclaim 不得选择
与 protected prefix 相交的 segment；正在迁移或写入的 source/incoming segment 也作为临时
protected set 传入。

请求结束后释放 request-level protection。后台 tick 在请求之前执行，因此普通后台维护不
继承尚未开始请求的 protection。

## 7. 两个并列的淘汰执行面

### 7.1 前台容量回收

前台回收只在某个具体动作无法获得目标容量时执行：

- Memory Dump admission；
- 后台 SLC 到 TLC migration；
- storage access 触发的 SLC 到 TLC migration。

```cpp
enum class CapacityCause {
    DumpAdmission,
    BackgroundMigration,
    AccessMigration,
};

struct CapacityPressureContext {
    TimestampNs timestamp_ns;
    CapacityCause cause;
    StorageTier target_tier;
    std::uint64_t required_blocks;
    std::span<const NodeId> protected_nodes;
};
```

`CapacityAction` 可以是：

- 从目标 pool 选择 leaf segment 并 Drop/Trim；
- 按具体 policy 选择迁往另一层级；
- 返回无合法 victim。

wear-share 与 adaptive-endurance policy 的 capacity eviction 必须保留其 leaf 选择、pin 和容量预算语义，不能替换
成通用高低水位淘汰。

### 7.2 独立后台维护

后台维护是 adaptive-endurance policy 的核心组成部分，不依赖新的 Dump，也不以“前台发现容量不足”为
触发条件。

Simulator 持有独立的虚拟后台调度器：

```cpp
void Simulator::run_until(TimestampNs target) {
    while (next_background_tick_ <= target) {
        drain_background_tick(next_background_tick_);
        next_background_tick_ += background_period_;
    }
}
```

在一个 tick 中：

```text
while true:
    action = storage_policy.next_background_action(tick, storage_view)
    if no action:
        break
    execute action
    commit Radix/Storage/Metrics/Trace
    notify policy
```

这样即使数小时没有请求，只要后续调用 `run_until()` 或 `finish(simulation_end)`，中间每个
后台 tick 仍在自己的逻辑 timestamp 执行，不会被压缩到下一个请求时间。

接口为：

```cpp
void process_request(const Request& request) {
    run_until(request.timestamp_ns);
    process_request_body(request);
}

void finish(TimestampNs simulation_end_ns) {
    run_until(simulation_end_ns);
    trace_writer.finish();
}
```

adaptive-endurance 默认后台周期为 900 秒，并通过配置显式暴露。period 为零表示该 policy 不启用
后台 tick。

### 7.3 Adaptive-endurance 后台选择顺序

每个 tick 使用当前全局状态重新计算：

```text
idle_threshold = clamp(idle_multiplier * global_gap_q95, 60 s, 6 h)
```

当 SLC utilization 超过 `slc_soft_util` 时，按 `occupancy_decay` 收缩阈值，但最终仍不小于
60 秒。水位只影响阈值，不是后台淘汰是否运行的开关。

Policy 同时检查：

- 全部 pool 中最老且超过 idle threshold 的 storage leaf；
- SLC 中达到动态 promotion age 的 segment。

如果两类动作同时到期，按各自 due timestamp 选择更早者；相同 due timestamp 使用稳定
NodeId/sequence 规则打破平局。每次提交后重新计算阈值和候选，直到当前 tick 无到期动作。

## 8. SLC 到 TLC Relocation

Relocation 是一等管理意图，但不是第四种物理 I/O 类型。Policy 只表达“把哪个已驻留 segment
从当前 placement 移到哪里”；Simulator 负责把意图统一降低为 READ/WRITE/TRIM。DWPDSim trace
和 MQSim 接口中不得出现 `MIGRATE` opcode。

```cpp
struct RelocateIntent {
    NodeId source_segment_endpoint;
    Placement destination;
    RelocationCause cause;
};
```

`RelocateIntent` 不携带 payload，也不由 Policy 决定如何拼装 I/O。Simulator 为已接受的意图
分配唯一 `MoveId`，从 StorageView 解析 source placement 和完整 segment，并判断当前 access
是否已经产生可复用的 source READ。

迁移前必须为完整 source segment 在 TLC 中获得容量。容量不足时使用
`CapacityCause::BackgroundMigration` 或 `CapacityCause::AccessMigration` 触发同步回收。

默认按物理搬迁开销将 relocation 展开为：

```text
READ(source) -> WRITE(destination) -> TRIM(source)
```

- storage hit 触发的 relocation 复用本次 access 已发出的 source READ，不重复读取；
- 后台 timer 触发的 relocation 显式生成 source READ，即使模拟器不搬运真实 payload，也要
  计入后台搬迁占用的读带宽和设备时间；
- 物理执行依赖为 source READ completion → destination WRITE completion → source TRIM；
- Core 按 READ、WRITE、TRIM 的 trace commit 顺序更新逻辑状态：destination WRITE 记录成功后
  修改唯一 storage location，source TRIM 随后释放旧地址；这不等价于声称下游物理 I/O 已完成；
- relocation WRITE 计入目标 tier/stream 的 program bytes；source READ 必须与普通请求 READ
  分开统计。

同一次 relocation 的原有或新生成 READ、WRITE、TRIM 共享 `move_id`。新生成的操作携带
relocation reason；复用的 access READ 保留 `StorageHit` reason，以免污染请求读统计。每个
后继 I/O 通过 `depends_on_sequence` 指向前驱；复用 access READ 时，WRITE 直接依赖该 READ
的 sequence。Policy 不观察这些低层 I/O，也不维护迁移状态机。

低层 I/O 展开、依赖链和 trace sequence 由 Simulator 完成，Policy 只决定 victim、目标层级
和 stream。Core 提交的是逻辑顺序，不等待离线 MQSim 的 completion。若 MQSim converter 只能
保证提交顺序、不能等待前驱 completion，则该模式只能用于 program bytes、DWPD 和资源竞争的
顺序化近似，结果不得声明为准确的 relocation latency。准确时延评估需要 dependency-aware
replay 按 completion 链依次提交三条操作。

## 9. Storage Policy 实现

### 9.1 WearShareRoundRobinStoragePolicy

- 使用 wear-share 规则选择 SLC/TLC；
- 在选定 pool 内使用 round-robin stream；
- Dump 空间不足时执行同步 capacity eviction；
- 不启用 idle/promotion 后台维护。

### 9.2 WearShareAffinityStoragePolicy

- 使用 wear-share 规则选择 SLC/TLC；
- 使用 affinity，缺失时使用 segment identity，稳定散列到 pool 内 stream；
- Dump 空间不足时执行同步 capacity eviction；
- 不启用 idle/promotion 后台维护。

### 9.3 AdaptiveEnduranceStoragePolicy

- 根据 SLC/TLC capacity 与 erase budget 形成 endurance-weighted 目标；
- 根据累计 program bytes、目标偏差和 occupancy pressure 决定初始 SLC/TLC placement；
- 使用稳定 affinity hash 选择 stream；
- 每个 request 只观察一次 session gap；
- storage logical access 更新 leaf recency；
- SLC segment 达到动态 age 时，access 路径可以触发迁移；
- 后台 tick 独立执行 idle leaf eviction 和到期迁移；
- 所有 admission/migration 在目标容量不足时执行同步 capacity eviction；
- placement、capacity eviction、background maintenance 和 program-byte 统计共享同一份
  policy 状态。

默认算法参数从算法参考提取并显式进入新配置：

```text
total streams                  = 6
adaptive-endurance SLC streams = 5
idle multiplier               = 32
promotion seconds             = 14400
adaptation gain               = 2
direct gain                   = 1
SLC soft utilization          = 0.75
occupancy decay               = 8
logical fill fraction         = 0.98
SLC erase budget              = 120
TLC erase budget              = 12
background timer period       = 900 s
```

DWPDSim 的 stream id 以 tier-local domain 表达时，policy 必须将参考算法的全局 stream 划分映射
到对应的 tier-local id，不能把全局 0..5 直接写入现有 tier-local trace 字段。

## 10. Baseline Policy

旧 policy 源码和 ABI 不保留。下一版本重新实现算法等价的 baseline：

```text
BaselineMemoryLruPolicy
BaselineFixedLruStoragePolicy
BaselineRatioLruStoragePolicy
```

baseline 使用与其他 policy 相同的 RequestContext、DumpContext、StorageView、后台
调度器和 commit 协议：

- Memory LRU 保留 storage-hit admission，并固定选择向 parent segment 贪婪的 Dump；
- Fixed baseline 使用固定 tier/stream placement 和 leaf-LRU capacity reclaim；
- Ratio baseline 使用目标 SLC write ratio、tier-local stream round-robin 和 leaf-LRU capacity
  reclaim；
- baseline 默认不产生周期后台动作；
- baseline 的价值是提供实验对照，不是兼容旧配置、类名、trace schema 或逐调用行为。

## 11. StorageView 与 Commit

```cpp
struct StorageView {
    const RadixTree& tree;
    const StorageState& storage;
    std::uint64_t block_size_bytes;

    SegmentView resolve_segment(NodeId endpoint) const;
    bool is_storage_leaf(NodeId endpoint) const;
    std::uint64_t resident_blocks(NodeId endpoint, StorageTier tier) const;
    bool intersects_protected(
        NodeId endpoint,
        std::span<const NodeId> protected_nodes
    ) const;
};
```

StorageMutation 区分：

```text
DumpWriteCommitted
CapacityTrimCommitted
IdleTrimCommitted
RelocationReadCommitted
RelocationWriteCommitted
RelocationSourceTrimCommitted
StorageAccessCommitted
NodePruned
```

`on_commit()` 是 policy 更新累计写入量、LRU、promotion index 和 residency-derived candidates
的唯一入口。Policy 不根据“计划返回成功”推断动作已经发生。

## 12. Trace 与 Metrics

### 12.1 Canonical trace ABI

canonical trace 固定为 schema version 4，CSV header 的列名和顺序如下：

```text
sequence,timestamp_ns,request_id,access_sequence,operation,storage_tier,stream_id,offset_bytes,length_bytes,node_id,hash_id,reason,move_id,depends_on_sequence
```

- `sequence` 是 DWPDSim semantic I/O 的全局严格递增编号，不是上层 `request_id`，也不是
  MQSim command id；
- `timestamp_ns` 保留全局绝对纳秒时间，不按 tier 或 flow 归零；
- 后台事件的 `request_id` 和 `access_sequence` 为空，前台事件保留其上层请求上下文；
- `storage_tier` 只取 `SLC`/`TLC`，`stream_id` 是 tier-local id；
- `offset_bytes` 是对应 pool-local allocator 的真实地址，converter 不按 NodeId 重映射；
- `operation` 只取 `READ`、`WRITE`、`TRIM`；
- 非 relocation I/O 的 `move_id` 和 `depends_on_sequence` 为空。

每个 relocation block 都形成独立的直接依赖链：

```text
READ(source) -> WRITE(destination) -> TRIM(source)
```

同一 segment 的全部 block 共享一个 `move_id`，但不增加 segment-wide barrier。source READ 与
source TRIM 必须具有相同 SLC tier、stream、LBA 和长度，destination WRITE 位于 TLC 且长度
相同。access relocation 恰好复用一个 `StorageHit` READ；同一 segment 的其他 source block
仍显式产生 `AccessMigration` READ。后台和容量 relocation 的每个 block 都显式产生 source
READ。

### 12.2 Trace reason

trace reason 区分：

```text
StorageHit
MemoryDump
CapacityEviction
IdleEviction
AccessMigration
BackgroundMigration
```

后台动作使用后台 tick 的 timestamp。前台 capacity eviction 使用触发它的 Dump 或 migration
timestamp。所有 I/O 保持严格 sequence 顺序。

物理 trace 的 operation 仍然只有 `READ`、`WRITE` 和 `TRIM`。Relocation 通过以下正交字段
表达，不增加 `MIGRATE` operation：

```text
move_id
reason = CapacityEviction | AccessMigration | BackgroundMigration
depends_on_sequence
```

非 relocation I/O 的 `move_id` 和 `depends_on_sequence` 为空。复用的 access READ 是例外：
它保留 `StorageHit` reason，同时通过非空 `move_id` 参与 relocation。MQSim converter 必须
保留 relocation READ/WRITE/TRIM 的 operation、stream、LBA、reason 和依赖关系，不能把三条
记录重新合并成自定义设备命令。

### 12.3 MQSim converter 与 completion dependency

converter 同时校验 canonical header、metrics `trace.schema_version == 4` 和完整转换时的
`trace.events` 行数。它读取一个同时包含 `slc`、`tlc` pool 的 SSD XML，要求 pool id 精确匹配、
容量与 DWPDSim metrics 一致、channel 集合不相交、media profile 引用有效且 measurement
window 满足 `start < end`。设备固定使用 NVMe、FLASH、PAGE_LEVEL mapping 并关闭
preconditioning；`slc`/`tlc` pool 分别引用 SLC/TLC media profile。SSD 配置内容的 SHA-256
和 MQSim 使用的标准 FNV-1a64 raw-XML hash（algorithm id `fnv1a64-raw-xml`）都冻结在
manifest 中。

flow id 固定映射为全部 SLC stream 后接全部 TLC stream，未产生 I/O 的 stream 也生成空 trace。
V1 使用 MQSim 的 8 个 NVMe queue pair，因此配置的 SLC/TLC stream 总数不得超过 8。所有 flow
共享一个 workload、一个 SSD 配置和同一绝对纳秒时间轴。

converter 为实际 MQSim command 分配 scenario 内全局连续 `command_request_id`。单条 semantic
I/O 超过 65535 sectors 时按连续地址拆分；manifest 为每个 command 记录 source sequence、chunk
index/count、pool、stream 和原地址。MQSim trace 的最后一列为
`depends_on_request_ids`，无前驱写 `-1`，否则写逗号分隔的一个或多个 command id。首 chunk
继承 semantic predecessor 的末 chunk，后续 chunk 依赖当前 semantic I/O 的前一 chunk。

converter 还对每个确定的 `(tier, tier_local_stream_id, chunk range)` 维护地址顺序：

- READ 只依赖最后一次 WRITE/TRIM，并加入未完成 reader 集合；
- WRITE/TRIM 依赖最后一次 WRITE/TRIM 及全部未完成 READ，随后清空 reader 集合并成为新的
  mutation；
- 不同 stream 的同值 pool-local LBA 属于独立 mapping domain，不互相增加依赖；
- semantic、chunk 和地址前驱合并并去重，因此 relocation destination WRITE 可以同时等待
  source READ 与目标地址旧 TRIM。

MQSim result 按显式 Flow_ID、Pool_ID 和 Channel ID 对账，不依赖 XML 元素顺序。整数计数只接受
十进制整数。summary 校验 configuration hash、measurement window、pool capacity 和逐 command
请求/字节总数，并保留 DWPDSim dump-only host-write bytes 与 MQSim 全部 destination WRITE bytes
两个不同口径。

对每个 pool，summary 使用 MQSim measurement-window 原始字段计算：

```text
measurement_days = (end_ns - start_ns) / 86400e9
host_DWPD = Measurement_Host_Write_Bytes / Logical_Capacity_Bytes / measurement_days
nand_DWPD = Measurement_Flash_Programmed_Bytes / Logical_Capacity_Bytes / measurement_days
write_amplification = Measurement_Flash_Programmed_Bytes / Measurement_Host_Write_Bytes
max_block_PE_per_day = Measurement_Max_Block_Erase_Count / measurement_days
```

任一分母为零时对应派生值为 `null`。

### 12.4 Metrics

metrics 记录：

- Dump requests、admitted/rejected segments、blocks 和 bytes；
- foreground capacity eviction segments、blocks 和 bytes；
- background ticks；
- background idle eviction segments、blocks 和 bytes；
- access-triggered 与 background-triggered migration；
- relocation source READ、destination WRITE 和 source TRIM 的 blocks/bytes，并区分 access READ
  复用与后台显式 READ；
- SLC/TLC live bytes 与 peak bytes；
- SLC/TLC host/program bytes；
- 每个 stream 的 WRITE bytes；
- adaptive-endurance gap samples、q95、当前 idle threshold；
- placement 到 SLC/TLC 的 segment、block 和 byte 数；
- `NoSpace`、protected-victim exhaustion 和 admission rejection。

前台容量淘汰和后台淘汰必须分别统计，不能合并为一个 storage eviction 计数。

## 13. 配置和 Python API

vNext 采用以下破坏性配置结构：

```python
SimulationConfig(
    block_size_bytes=...,
    memory=MemoryConfig(...),
    slc=StorageTierConfig(...),
    tlc=StorageTierConfig(...),
    memory_policy=MemoryPolicyConfig(kind=...),
    storage_policy=StoragePolicyConfig(kind=...),
    simulation_end_ns=...,
)

Request(
    timestamp_ns=...,
    request_id=...,
    affinity_id=...,
    hash_ids=[...],
)
```

删除独立的 `PlacementPolicyConfig` 和 `StorageEvictionPolicyConfig`。storage policy 的配置
必须在一个对象中表达 placement、capacity、wear 和 background 参数，避免组合出状态不
一致的实现。

批量输入必须携带：

- timestamp buffer；
- request id buffer；
- affinity id buffer；
- offsets；
- ordered hash ids。

批量接口与逐 request 接口必须产生相同的后台 tick、决策、metrics 和 trace。

## 14. 实现阶段

### 阶段一：新 Core 接口

- 删除旧三套 policy ABI 和配置；
- 引入 RequestContext、DumpContext、StorageView 和 commit 协议；
- 明确 top-to-endpoint segment order；
- 增加 storage-residency-aware leaf 查询；
- 建立虚拟后台调度器和 `finish(simulation_end_ns)`。

### 阶段二：Baseline

- 重新实现 Memory LRU；
- 重新实现 Fixed+LRU 和 Ratio+LRU storage baseline；
- 建立小规模确定性 functional tests。

### 阶段三：Wear-share policies

- 移植 wear-aware tier placement；
- 移植 RR/affinity-hash stream placement；
- 移植同步 capacity eviction；
- 对照算法参考验证 placement 和 victim sequence。

### 阶段四：Adaptive-endurance

- 移植 request gap estimator；
- 移植 dynamic idle/promotion threshold；
- 实现 access-triggered migration；
- 实现周期后台 idle eviction 和 migration；
- 验证长请求空洞期间的 tick timestamp 和动作顺序。

### 阶段五：Trace 与 MQSim

- 扩展 trace reason、`move_id` 和 `depends_on_sequence`；
- 将 `RelocateIntent` 统一降低为 READ/WRITE/TRIM，不引入 `MIGRATE` opcode；
- 更新 MQSim converter；
- 分离前台/后台 metrics；
- 使用同一 synthetic workload 对照算法参考的决策结果；

## 15. 验收条件

实现满足：

1. 仓库中不存在旧 placement/storage-eviction policy ABI；
2. 所有 policy 都运行在同一套新接口上；
3. 只有 Memory Dump 能触发新的 storage admission；
4. Memory `Drop` 只剪枝选中的 leaf segment；`Dump` 从该 leaf 开始，以完整 segment 为单位
   向根贪婪回收，并在第一个含未写盘 block 的 segment 停止；
5. 一个 Dump segment 只产生一次 placement；
6. Dump 写入前按完整 bytes 获取容量，不产生 partial write；
7. 前台 capacity eviction 与后台 idle eviction 分别运行和计数；
8. 无前台请求的时间区间仍按周期执行后台 tick；
9. 同时间 tick 先于当前 request 更新 adaptive-endurance gap；
10. 后台 migration 能独立触发 TLC capacity reclaim；
11. 当前请求的 protected prefix 不会被前台回收；
12. Policy 不修改或复制 RadixTree、StorageState 和 LBA 状态；
13. program bytes 只在实际 WRITE commit 后增加；
14. batch 与逐 request 输入产生完全一致的逻辑结果；
15. 相同输入、配置和 simulation end 得到确定一致的 trace 与 metrics；
16. 后台 relocation 生成 READ、WRITE、TRIM，access-triggered relocation 复用已有 source
    READ；
17. relocation 的三条 I/O 共享 `move_id`；trace 记录 READ → WRITE → TRIM 依赖链，只有
    dependency-aware replay 才能宣称兑现 completion 依赖；
18. DWPDSim 和 MQSim 的 operation 集合中不存在 `MIGRATE` opcode；
19. 最终链路只包含 DWPDSim，不包含算法参考的运行时组件。
