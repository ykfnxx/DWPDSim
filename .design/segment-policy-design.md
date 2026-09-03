# DWPDSim RadixTree Policy 与 Segment 淘汰设计

状态：v0.5 已实现。

本文描述 DWPDSim v0.5 的 RadixTree、policy 和 segment 淘汰语义。它与
[`rewrite-design.md`](rewrite-design.md) 一起构成当前实现规范；访问、介质、trace 和
metrics 的其余语义以该文档为准。

## 1. 设计目标

当前设计满足两个目标：

1. policy 可读取 RadixTree 中的拓扑、访问统计和介质驻留状态；
2. 淘汰策略以 segment 为逻辑选择单位，而实际状态变更和 I/O 仍以 block 为单位。

实现遵守以下边界：

- Simulator 是唯一的流程协调者和状态修改者；
- policy 读取状态并返回决策，不直接修改 RadixTree、介质、metrics 或 trace；
- 核心热路径全部留在 C++，不增加逐 block Python callback；
- 不复制整棵树或为 policy 创建状态快照；
- NodeId 对外稳定，内部存储使用独立的稠密槽位。

## 2. 已确认的核心语义

### 2.1 一棵全局 RadixTree

系统中只有一棵全局 RadixTree。Memory、SLC 和 TLC 分别是其节点集合的子集：

```text
M   ⊆ V(T)
SLC ⊆ V(T)
TLC ⊆ V(T)
```

其中 `T` 是全局 RadixTree。介质不会各自维护一棵独立的树，也不会分别定义自己的
segment 边界。

节点可以同时存在于 Memory 和一个 SSD 介质，但不能同时存在于 SLC 和 TLC。节点的
介质成员关系继续由以下状态表达：

```text
in_memory: bool
on_storage: bool
storage_medium: SLC | TLC
```

### 2.2 NodeId 直接使用输入 hash_id

输入 `hash_id` 被定义为全局唯一的逻辑 block 标识：

```cpp
using HashId = std::uint64_t;
using NodeId = HashId;
```

节点身份只由 `hash_id` 确定。`parent_id` 用于表达 RadixTree 拓扑、向上遍历和 segment
边界。

输入必须满足：

- 同一个 hash 始终表示同一个逻辑 block；
- 同一个 hash 不会合法地出现在不同 parent 下；
- hash 已包含调用方需要的完整前缀身份语义；
- uint64 hash 碰撞不在 DWPDSim 的模拟范围内。

root 不占用输入 hash 空间。实现中使用专用内部 root slot，不能要求调用方避开某个合法
uint64 hash。

“NodeId 不复用”表示 NodeId 永远不会被分配给另一个逻辑 block。同一个 block 在完全
删除后再次出现，仍使用相同的输入 hash 作为 NodeId，但开始一个新的统计生命周期。

### 2.3 节点删除时丢弃统计

Node 的访问统计与 RadixTree 节点具有相同生命周期：

```text
first_seen_timestamp
last_access_timestamp
last_hit_timestamp
access_count
```

节点从 RadixTree 中删除时，这些统计以及 policy 为该节点维护的派生状态一起丢弃。相同
hash 后续重新进入系统时按冷节点处理，不保留上一生命周期的统计。

如果一个节点已不在任何介质，但仍有存活 child，它暂时保留为拓扑节点，不能提前删除。
它的统计也保留到该结构节点实际被删除时为止。

## 3. Segment 定义

### 3.1 全局拓扑定义

segment 仅由全局 RadixTree 的当前拓扑确定，与节点属于 Memory、SLC 还是 TLC 无关。

`child_count != 1` 的非 root 节点是 segment leaf，也称 segment 端点。端点有两类：

- global leaf：`child_count == 0`；
- branch endpoint：`child_count >= 2`。

从端点沿 parent 向上遍历，包含连续的单 child 祖先，在 root 或另一个 branch parent 前
停止：

```text
          parent branch            不属于下游 segment
          /         \
         x          ...            x 是 segment_top
         |
         y
         |
    leaf or branch endpoint         choose_victim() 返回值

segment = [endpoint, ..., y, x]
```

分叉节点不属于其 child 的下游 segment，而是作为自身上游 segment 的端点。这样每个真实
节点恰好属于一个 segment，不会因为分叉节点没有归属而出现无法释放的驻留 block。当删除
使 `child_count` 从 2 变为 1 时，该节点不再是端点，原上下游 segment 在后续解析中自然
合并。

### 3.2 Segment 与介质子集

segment 是全局逻辑对象，介质操作作用于 segment 与目标介质的交集：

```text
Memory eviction: Segment(endpoint) ∩ M
SLC eviction:    Segment(endpoint) ∩ SLC
TLC eviction:    Segment(endpoint) ∩ TLC
```

因此 policy 返回的端点本身不要求位于目标介质，但必须满足：

```text
Segment(endpoint) ∩ target_medium != ∅
```

这样端点可以稳定表示一个全局 segment，而不会因为同一 segment 的 block 分布在不同
介质上就生成多套 segment 身份。

## 4. Policy 只读访问 RadixTree

### 4.1 访问方式

Simulator 在 policy 调用中显式传入 `const RadixTree&`：

```cpp
virtual NodeId choose_victim(
    const AccessContext& context,
    const RadixTree& tree
) = 0;
```

StorageEvictionPolicy 保留现有返回语义，只增加只读 tree：

```cpp
virtual NodeId choose_victim(
    Medium medium,
    NodeId incoming_node,
    const AccessContext& context,
    const RadixTree& tree
) = 0;
```

需要访问树统计的其他决策接口也直接接收 `const RadixTree&`：

```cpp
admit_storage_hit(context, node, tree) -> bool
eviction_action(victim, context, tree) -> DROP | PERSIST
place(node, context, tree, storage_summary) -> Placement
```

实现不增加通用插件上下文、动态注册系统或长期保存的 tree 指针。按调用传只读引用可以
明确生命周期，也不会复制节点或统计。

### 4.2 Policy 可以读取的数据

RadixTree 至少提供以下只读查询：

```cpp
const Node& node(NodeId node_id) const;
std::optional<NodeId> parent(NodeId node_id) const;
std::uint32_t child_count(NodeId node_id) const;
bool is_leaf(NodeId node_id) const;
NodeId segment_leaf_for(NodeId node_id) const;
NodeId segment_top(NodeId endpoint) const;
void resolve_segment(NodeId endpoint, std::vector<NodeId>& segment) const;
bool contains(NodeId node_id) const;
```

policy 可以据此读取：

- 当前节点、候选节点及祖先的访问统计；
- 全局 parent/child/branch/leaf 关系；
- segment 边界；
- 节点是否在 Memory、SLC 或 TLC；
- segment 与目标介质的交集。

policy 不得保存跨调用的 `Node&` 或 `Node*`。它只能保存 NodeId 或自身派生状态，并在每次
使用时重新通过 RadixTree 查询节点。

### 4.3 拓扑生命周期通知

segment 候选会随节点创建、删除和 branch 合并而变化。为了避免 policy 在
`choose_victim()` 中扫描整棵树，Simulator 必须通知需要维护候选集合的 policy：

```cpp
on_node_created(node_id, parent_id, tree_after_change)
on_node_removed(node_id, parent_id, tree_after_change)
```

调用顺序为：

- `on_node_created`：节点和 parent edge 已加入 tree 后、任何由本次访问触发的淘汰决策前；
- `on_node_removed`：节点和 edge 已移除、parent 的 `child_count` 已更新后；
- 所有 policy 都只读取变化后的 tree；
- 被删除节点的必要身份通过参数传入，policy 不再从 tree 查询该节点。

MemoryPolicy 和 StorageEvictionPolicy 可以使用这些事件维护各自的 segment 候选、LRU 或
评分结构。WritePlacementPolicy 不维护候选集合，但在 `place()` 时仍可读取 tree 统计。

### 4.4 访问统计时序

一次 block access 使用两个明确阶段：

```text
1. get_or_create，并完成拓扑通知
2. 判断 MemoryHit / StorageHit / GlobalMiss
3. policy 决策读取“本次访问前”的历史统计
4. Simulator 执行介质状态变化和 I/O
5. RadixTree 更新本次访问统计
6. policy 接收本次访问完成通知
7. MetricsCollector 记录访问结果
```

访问完成通知为：

```cpp
on_access_complete(context, result, tree_after_access)
```

这样 admission、victim 和 placement 决策不会混淆本次访问前后的统计；需要更新在线评分
或预测状态的 policy 可以在 `on_access_complete` 中读取已经包含本次访问的数据。

新创建节点在决策阶段的 `access_count == 0`。`first_seen_timestamp` 可以等于当前
timestamp，但不能把它解释为一次已经完成的历史访问。

## 5. Memory Segment 淘汰

### 5.1 选择和展开

内存空间不足时只调用一次 MemoryPolicy：

```text
endpoint = memory_policy.choose_victim(context, tree)
segment = tree.resolve_segment(endpoint)
```

MemoryPolicy 返回全局 segment 端点，并保证 segment 中至少有一个 memory-resident block。
Simulator 负责展开 segment，policy 不返回 block vector，也不直接修改状态。

### 5.2 逐 block 行为

Simulator 按 `endpoint -> segment_top` 顺序遍历 segment：

```text
for node in segment:
    if node not in Memory:
        continue

    if node has SLC/TLC copy:
        remove Memory copy
        do not WRITE
    else:
        action = memory_policy.eviction_action(node, context, tree)
        if action == PERSIST:
            placement = placement_policy.place(node, context, tree, storage_summary)
            WRITE node as one block
        else:
            DROP node

    remove node from Memory
```

SSD 副本是否存在只影响单个 block 的实际动作，不改变 segment 成员，也不会中途拆分
segment 或再次调用 `choose_victim()`。

一次 segment 淘汰可能释放多个 memory block。当前 admission 只需要一个 block，额外释放
的容量继续作为空闲容量保留。

## 6. Storage Segment 淘汰与 SLC 迁移

WritePlacementPolicy 选择目标 medium 后，如果该 medium 没有空闲 block：

```text
endpoint = storage_policy.choose_victim(medium, incoming_node, context, tree)
action = storage_policy.eviction_action(medium, endpoint, incoming_node, context, tree)
segment = tree.resolve_segment(endpoint)
```

Simulator 按 `endpoint -> segment_top` 遍历：

```text
for node in segment:
    if node not in source medium:
        continue

    if action == DEMOTE_TO_TLC:
        ensure TLC has one free block, evicting one TLC segment if necessary
        emit TLC WRITE for this block
        emit source SLC TRIM for this block
        move this block storage location from SLC to TLC
    else:
        emit TRIM for this block
        release this block address
        clear this block storage location
```

StorageEvictionPolicy 在选出端点后，为整个 segment 返回一次动作。内置 LRU 对 SLC 返回
`DEMOTE_TO_TLC`，对 TLC 返回 `DROP`。SLC 动作仅操作 segment 的 SLC 子集；原本已经位于
TLC 的成员不重复写入。TLC 动作也仅操作 TLC 子集。

SLC 迁移对每个 block 严格输出 `TLC WRITE -> SLC TRIM`，随后才更新该节点的唯一 storage
location。迁移目标 stream 由 WritePlacementPolicy 的 `place_on_medium(TLC, ...)` 选择；
这个强制介质写入不计入 ratio policy 的原始 SLC/TLC 写入配比。迁移期间节点始终至少有一份
盘上逻辑副本，因此不会删除 RadixTree 节点或访问统计。

TLC 满时先对一个 TLC segment 执行 DROP，再输出迁移的 TLC WRITE。一次 SLC segment 的
迁移可能多次触发 TLC 淘汰。SLC 和 TLC 使用各自的 segment scratch，避免嵌套淘汰覆盖外层
SLC segment 快照。

TRIM、地址释放和 trace 仍逐 block 发生。segment 不是一条合并后的 SSD I/O，也不要求
segment block 在地址空间中连续。迁移不产生额外 READ；通用 trace 只表达 TLC 目标写入和
SLC 源位置失效，不模拟真实数据传输或跨设备完成依赖。

## 7. Segment 快照与嵌套状态变化

segment 必须根据 `choose_victim()` 返回时的拓扑一次性确定。不能一边删除节点一边重新
判断 parent 是否为分叉，因为删除 child 会改变 `child_count`，从而越过原 segment
边界。

Simulator 使用可复用 scratch buffer 保存 NodeId；memory 使用一份，SLC/TLC storage
各使用一份：

```text
memory_segment = [endpoint, ..., segment_top]
storage_segment[medium] = [endpoint, ..., segment_top]
```

scratch buffer 保存全局 NodeId，不保存内部 NodeSlot，也不保存 `Node&`。逐 block 执行
期间可能因为 SLC 迁移 WRITE 触发嵌套的 TLC segment eviction；按介质分离 buffer 保持
外层 SLC 快照不变。全局 NodeId 不会被分配给其他 block，因此不会因为内部 slot 回收而
错误引用新节点。

整个 segment 的介质操作结束后再执行统一的拓扑 prune。DWPDSim 不为 segment 建立事务、
回滚或失败后继续运行机制；发生无法继续的输出或状态错误时直接终止模拟。

正在处理的 access node 在 admission 完成前不参与 prune。memory eviction 中嵌套触发的
storage segment 会先完成 TRIM 和地址释放，但把结构 prune 延迟到外层 memory segment
结束，避免复用 slot 后使外层 NodeId 快照引用错误对象。

## 8. RadixTree 删除与向上 Prune

### 8.1 删除条件

节点可以从 RadixTree 删除的必要条件是它不在任何介质：

```text
!in_memory && !on_storage
```

为了保持 parent/child 拓扑，实际删除还要求：

```text
child_count == 0
```

完整条件：

```text
prunable(node) =
    node != root
    && !node.in_memory
    && !node.on_storage
    && node.child_count == 0
```

如果节点已不在所有介质但仍有 child，它保留为拓扑节点。后继删除后，Simulator 从 leaf
向 root 递归 prune：

```text
while prunable(node):
    parent = node.parent
    erase node and parent edge
    parent.child_count--
    discard node statistics
    notify policies through on_node_removed(node_id, parent_id, tree_after_change)
    release internal slot after all policies have cleared the old node state
    node = parent
```

当 parent 的 `child_count` 从 2 降到 1 时，它不再是 branch。下一次 segment 解析将穿过
该节点，实现全局 segment 的动态合并。

### 8.2 NodeId 与内部槽位

输入 hash 是稀疏 uint64，不能直接作为 vector 下标。实现区分：

```cpp
using NodeId = std::uint64_t;    // 输入 hash，对外逻辑身份
using NodeSlot = std::uint32_t;  // 内部稠密存储位置
```

当前物理结构：

```text
node_index: unordered_map<NodeId, NodeSlot>
nodes: vector<NodeRecord>
free_slots: vector<NodeSlot>
root_slot: dedicated NodeSlot
```

NodeSlot 可以在节点删除后回收，但 NodeId 不会被用于另一个 block。policy、trace 和 segment
scratch buffer 不暴露 NodeSlot。

内置 LRU 使用显式的 NodeId 到 link 映射，不保存 NodeSlot。节点移出介质时清理 LRU link，
节点删除后再通过 topology 通知清理其他 policy 派生状态，随后回收 slot。

### 8.3 相同 hash 再次出现

一个节点完全删除后，相同 hash 可能在后续请求中再次出现：

```text
NodeId: 与原逻辑 block 相同
NodeSlot: 可以不同
parent: 必须符合全局 hash 输入契约
statistics: 全部从零开始
policy state: 冷启动
```

这不是把 NodeId 复用给另一个 block，而是同一逻辑 block 开始新的系统驻留生命周期。

## 9. Metrics 与 Trace

segment 选择和实际 block 行为分别计数。

当前新增指标：

```text
memory.evicted_segments
memory.evicted_blocks
storage.slc.evicted_segments
storage.slc.evicted_blocks
storage.tlc.evicted_segments
storage.tlc.evicted_blocks
storage.slc.demoted_segments
storage.slc.demoted_blocks
storage.tlc.demoted_segments
storage.tlc.demoted_blocks
tree.nodes_created
tree.nodes_removed
```

现有 READ、WRITE、TRIM、DROP、PERSIST、bytes 和 stream 指标继续按 block 计数。一个
segment 释放多个 block 时只增加一次对应的 `evicted_segments`。`evicted_*` 统计离开源
介质的全部 segment/block，包含 DROP 和迁移；`demoted_*` 是其中迁到 TLC 的子集，内置
策略下 TLC 的该组计数始终为零。

I/O trace 继续逐 block 输出，不增加虚构的 segment I/O 或 `segment_sequence`；segment
数量只在 metrics 中记录。SLC 迁移产生一条 TLC WRITE 和一条 SLC TRIM，两条 reason 均为
`SLC_DEMOTION`；该枚举扩展对应 trace schema version 2。

NodeId 直接等于 hash_id，trace 中 `node_id` 和 `hash_id` 数值相同，并保留两个字段以维持
现有 schema。

## 10. 关键不变量

实现必须保持：

1. 全系统只有一棵 RadixTree；介质只是节点子集；
2. NodeId 等于输入的全局唯一 hash，不能代表另一个 block；
3. policy 只能通过 const 接口读取 tree；
4. policy 返回 global segment 端点，Simulator 展开 segment；
5. segment 边界只由全局拓扑决定，不由介质驻留状态决定；
6. segment 在状态修改前完成快照；
7. segment 是逻辑批量单位，实际状态变化和 I/O 是 block 粒度；
8. SSD 副本状态不会拆分 memory segment；
9. storage eviction 只操作 segment 与源 medium 的交集；SLC 迁移不触碰原有 TLC 子集；
10. 节点不在所有介质且没有 child 时才可删除；
11. 节点删除时同时丢弃访问统计和 policy 派生状态；
12. NodeSlot 只能在 policy 清理旧状态后回收；
13. SLC 和 TLC 不保存同一节点的双副本；
14. root 不对应真实 block，也不参与 segment 淘汰；
15. SLC 迁移按 block 输出 TLC WRITE 后再输出 SLC TRIM，且不产生迁移 READ；
16. 嵌套 TLC 淘汰不能修改外层 SLC segment 快照。

## 11. 实现范围

当前实现修改：

- `cpp/include/dwpdsim/types.hpp`
  - NodeId/hash 语义、NodeSlot、child count 和节点生命周期字段；
- `cpp/include/dwpdsim/radix_tree.hpp`
  - NodeId 查找、segment 查询、节点删除和 prune 接口；
- `cpp/src/radix_tree.cpp`
  - 全局 hash 索引、slot 管理、child count 和结构删除；
- `cpp/include/dwpdsim/policies/`、`cpp/src/policies/`
  - 三类 policy 的独立基类/实现文件、只读 tree 参数、淘汰动作和生命周期通知；
- `cpp/include/dwpdsim/simulator.hpp`、`cpp/src/simulator.cpp`
  - segment 快照、逐 block 执行、统一 prune 和通知顺序；
- `cpp/include/dwpdsim/metrics.hpp`、`cpp/src/bindings.cpp`
  - segment 和节点生命周期指标；
- C++ integration tests 和 Python functional tests
  - 新语义的端到端验证。

Python 输入仍然是 timestamp 加有序 hash 序列，不增加逐节点 Python 对象或 callback。

## 12. 验证范围

集成测试覆盖以下场景：

1. policy 能读取当前节点、祖先和候选 segment 的历史统计；
2. 同一输入 hash 始终得到相同 NodeId；
3. 相同 hash 删除后重新出现时统计从零开始；
4. internal NodeSlot 回收不会改变外部 NodeId；
5. memory segment 同时包含有 SSD 副本和无 SSD 副本的 block；
6. 有副本 block 不重复 WRITE，无副本 block 按策略 DROP 或 PERSIST；
7. SLC 只迁移全局 segment 的 SLC 子集，TLC 只删除其 TLC 子集；
8. 一个 segment 释放多个 block 时容量和 block/segment metrics 正确；
9. 分叉节点不属于 child 的下游 segment，并作为自身上游 segment 的端点；
10. 删除一条分支后 branch 变成单 child，后续 segment 正确向上合并；
11. 所有介质都不存在但仍有 child 的节点保留为拓扑节点；
12. 最后一个 child 删除后向上递归 prune，并同时丢弃统计；
13. batch 和逐请求接口产生相同的 segment、metrics 和 trace；
14. storage hit 的 READ、SLC 迁移的 `TLC WRITE -> SLC TRIM`、TLC 满时的
    `TLC TRIM -> TLC WRITE` 和逐 block trace 顺序保持确定；
15. TLC 满时的嵌套淘汰不覆盖正在迁移的 SLC segment 快照。

性能基准需要额外记录：

- 每次 `choose_victim()` 的均摊开销；
- 平均和高分位 segment 长度；
- NodeId 到 NodeSlot 查询开销；
- topology notification 和 segment candidate 更新开销；
- 节点创建、删除和 slot 回收后的峰值 RSS。

性能数据仍由独立 benchmark 采集，不由正确性测试推断。
