# DWPDSim 整体架构与模拟流程

本文描述当前 DWPDSim 的已实现架构：请求如何进入模拟器，公共 RadixTree、DRAM、SLC/TLC
如何更新，MemoryPolicy 和 StoragePolicy 在什么位置介入，以及逻辑 I/O 如何交给 MQSim。运行配置
与命令见 [mqsim-pipeline.md](mqsim-pipeline.md)。

## 1. 模拟边界

DWPDSim 是 KV cache 管理算法的逻辑模拟器。它负责：

- 按请求构建和更新一棵公共 RadixTree；
- 维护 block 在 DRAM、SLC 和 TLC 中的逻辑驻留；
- 执行 MemoryPolicy 的 DRAM admission、victim 选择和 `Dump`/`Drop` 决策；
- 执行 StoragePolicy 的 dump placement、容量回收、访问迁移和后台维护决策；
- 把决策展开为确定性的 `READ`、`WRITE`、`TRIM` 语义 I/O；
- 统计命中、淘汰、下盘、迁移、容量和算法状态。

DWPDSim 不模拟 NAND page、FTL、GC、擦除、设备队列、I/O latency 或物理写放大。这些设备层行为
由 MQSim 回放 DWPDSim 的语义 I/O 后计算。因此，Policy 在 DWPDSim 内运行，MQSim 只接收已经
决定好的 I/O，不再执行 placement、淘汰或迁移算法。

## 2. 总体分层

```text
JSONL / workload adapter
  Request(timestamp_ns, request_id, affinity_id, hash_ids)
                         |
                         v
Python DWPDSimulator + SimulationConfig
  - 配置解析、逐请求/批量输入、进度日志、metrics JSON
                         |
                         v  pybind11
C++ Simulator ------------------------------------------------------+
  |                                                                 |
  +-> RadixTree：拓扑、节点访问统计、DRAM/SSD 驻留真值             |
  +-> StorageState：SLC/TLC 容量、pool-local block address          |
  +-> MemoryPolicy：DRAM admission、leaf victim、Dump/Drop          |
  +-> StoragePolicy：placement、capacity reclaim、migration、tick   |
  +-> MetricsCollector：逻辑命中、淘汰和 I/O 计数                   |
  +-> TraceWriter：canonical READ/WRITE/TRIM                         |
                         |                                           |
                         +-------------------------------------------+
                         |
                         v
simulation_trace.csv + simulation_metrics.json
                         |
                         v
MQSim converter
  - 按 SLC/TLC 与 stream 生成 flow
  - 切分大 I/O、补全依赖、生成 workload.xml 和 manifest.json
                         |
                         v
MQSim
  - NVMe/FTL/NAND/GC/latency/wear 模拟
                         |
                         v
workload_scenario_1.xml + summary.json
```

热路径位于 C++。Python 层不对每个 block 回调 policy；大数据集通过连续 `uint64` buffer 调用
`process_batch()`，其语义与逐请求 `process()` 相同。

## 3. 核心数据模型

### 3.1 Request

每条请求包含：

| 字段 | 含义 |
| --- | --- |
| `timestamp_ns` | 全局虚拟时间，跨请求非递减 |
| `request_id` | 本轮模拟内唯一的请求编号 |
| `affinity_id` | session/conversation 身份，用于 affinity stream 和 adaptive gap |
| `hash_ids` | 当前请求完整、有序的 Radix 路径 |

模拟器按 `hash_ids` 的输入顺序逐 block 处理，不会在第一个 miss 后停止。每个 `hash_id` 同时是
全局唯一 `NodeId`，一个节点对应一个固定大小的逻辑 block；parent link 只表达前缀拓扑。

### 3.2 一棵公共 RadixTree

Memory、SLC 和 TLC 没有各自独立的树。全系统只有一棵 `RadixTree`，每个节点保存：

- `in_memory`：是否存在 DRAM 副本；
- `on_storage`：是否存在 SSD 副本；
- SSD 副本所在的 `storage_tier`、`stream_id` 和 pool-local `block_address`；
- first seen、last access、last hit 和 access count；
- parent/children 拓扑。

一个节点可以同时有 DRAM 副本和一个 SSD 副本，但最多只能有一个 SSD location。因此三个层级是
同一全局节点集合上的驻留子集，而不是三套独立 identity。

### 3.3 Segment

Policy 以 segment 选择候选，Simulator 再将其展开到 block：

- segment 是从 `segment_top` 到 `segment_endpoint` 的有序节点链；
- 根边界或 parent 出现分叉时形成新的 segment；
- memory leaf segment 与 DRAM 有交集，且没有仍在 DRAM 的后代；
- storage leaf segment 与目标 tier 有交集，且没有仍在 storage 的后代。

选择、LRU 和迁移原因以 segment 为单位；地址分配、驻留位、trace 和多数 I/O 计数以 block 为单位。

### 3.4 状态所有权

`Simulator` 是所有共享状态和输出的唯一修改者：

| 组件 | 拥有内容 |
| --- | --- |
| `RadixTree` | 节点、拓扑、访问统计和驻留真值 |
| `StorageState` | SLC/TLC 已用容量和 pool-local 地址分配器 |
| `MetricsCollector` | 命中、驻留、淘汰、迁移和语义 I/O 计数 |
| `TraceWriter` | 全局有序的 canonical I/O trace |
| Policy | 候选索引和算法派生状态，不拥有共享驻留真值 |

Policy 通过只读 `RadixTree` 或 `StorageView` 观察当前状态，只返回 decision/intent。Simulator 执行成功
并更新共享状态后，再通过 `on_commit()` 通知 policy。Policy 不直接分配地址、不修改节点、不写 trace。

## 4. 初始化

Python `DWPDSimulator` 将 `SimulationConfig`、MemoryPolicy 配置和 StoragePolicy 配置转换为 C++
对象。C++ 初始化顺序为：

1. 校验 block size、DRAM/SLC/TLC 容量和 stream 数；
2. 按容量创建 DRAM block limit 和两个 `StorageTierState`；
3. 创建一棵空 RadixTree；
4. 创建一个 MemoryPolicy 和一个 StoragePolicy；
5. 打开 canonical trace；
6. 从 StoragePolicy 的 `background_schedule()` 取得第一个虚拟后台 tick。

当前只提供 `baseline_lru` MemoryPolicy；StoragePolicy 五选一。两类 policy 在同一模拟器中组合：
MemoryPolicy 决定 DRAM 如何管理，StoragePolicy 决定写到 SSD 后如何管理。

## 5. 单条请求的完整流程

### 5.1 推进虚拟时间

处理 timestamp 为 `T` 的请求前，Simulator 先执行所有 `tick_time <= T` 的后台 tick。因此同一
timestamp 上，后台动作先于请求访问。没有后台周期的 policy 直接跳过此阶段。

这里的“后台”是虚拟时间上的同步维护，不是独立 OS 线程。只有新的请求 timestamp 或 `finish()`
将模拟时间向前推进时，后台 tick 才会执行。

### 5.2 建立请求上下文

Simulator 沿输入 hash path 查找已有节点，收集从根开始连续存在于 DRAM 或 SSD 的
`protected_prefix`。这个前缀会在本请求引起的 storage capacity reclaim 中受到保护，避免为当前
请求腾空间时删除正在使用的存储数据。

随后调用：

```text
StoragePolicy::on_request_begin(request, storage_view)
```

普通 storage policy 不处理该事件；`adaptive_endurance` 用它按 `affinity_id` 观察 session gap，
更新全局 q95 gap 估计。`affinity_id=0` 时，此处使用 `request_id` 作为 session identity。

### 5.3 构建路径并逐 block 访问

Simulator 从根到叶依次执行：

1. 按 `(parent, hash_id)` 查找节点；不存在时创建节点并接入 RadixTree；
2. 构造包含 request、position、parent 和全局 access sequence 的 `AccessContext`；
3. 根据 `in_memory`、`on_storage` 进入 memory hit、storage hit 或 global miss 分支。

#### Memory hit

```text
node.in_memory = true
  -> 记录 memory hit 和节点访问时间
  -> MemoryPolicy::on_commit(Accessed)
  -> 不产生 I/O
```

#### Storage hit

```text
node.in_memory = false, node.on_storage = true
  -> 记录 SLC/TLC hit 和访问时间
  -> StoragePolicy::on_commit(StorageAccessCommitted)
  -> StoragePolicy::on_storage_access(...)
       | no action       -> 发出本 block 的 STORAGE_HIT READ
       | RelocateIntent  -> 检查目标容量并执行 segment relocation
  -> MemoryPolicy::admit_storage_hit(...)
       | true  -> 插入 DRAM，必要时先触发 DRAM 淘汰
       | false -> 保持 storage-only
```

`adaptive_endurance` 可以在老化 SLC segment 被访问时返回 SLC 到 TLC 的 access migration。当前
storage-hit READ 会作为被访问 block 的 relocation source READ，其余同 segment block 再产生显式
READ，避免为当前 block 重复读。

#### Global miss

```text
node.in_memory = false, node.on_storage = false
  -> 记录 global miss 和节点访问时间
  -> 将新 block 插入 DRAM
  -> 不产生 READ，也不直接 WRITE SSD
```

Global miss 表示该 KV block 由上层计算产生。DWPDSim 当前没有独立的 prefill-create opcode；只有
后续 DRAM 淘汰决定 `Dump` 时，新 block 才会成为 StoragePolicy 的输入。

## 6. MemoryPolicy 介入点

MemoryPolicy 接口只有三个职责：

| 接口 | 调用位置 | 返回/作用 |
| --- | --- | --- |
| `admit_storage_hit()` | storage READ 或 access migration 后 | 决定 storage hit 是否进入 DRAM |
| `evict()` | 插入 block 时 DRAM 已满 | 返回 memory leaf endpoint 和 `Dump`/`Drop` |
| `on_commit()` | DRAM 插入、命中、移除之后 | 更新 policy 自己的 LRU/派生状态 |

当前 `baseline_lru`：

- 插入和访问的 block 移到 LRU 前端；
- 淘汰时选择最久未访问的 memory leaf segment；
- `admit_storage_hits` 控制 storage hit 是否进入 DRAM；
- 淘汰 action 固定返回 `Dump`。

### 6.1 当前 Dump/Drop 执行语义

`Drop` 只处理 policy 选中的 leaf segment：移除其中所有 DRAM-resident block，不产生 storage
WRITE，然后停止。

`Dump` 当前从选中的 leaf segment 开始向 parent segment 贪婪回收。对每个 segment 计算：

```text
memory_nodes = segment ∩ DRAM
write_nodes  = {node in memory_nodes | !node.on_storage}
```

- `write_nodes` 为空：只移除该 segment 的 DRAM 副本，继续处理 parent segment；
- `write_nodes` 非空：将缺少 SSD 副本的 block 作为一次 segment dump，随后移除该 segment 的
  DRAM 副本并停止；
- dump admission 失败：该 segment 仍从 DRAM 移除，并计入 rejected dump 和 drop；
- 已有 SSD 副本的 block 不重复 WRITE。

因此当前一次 `Dump` 决策可能从 DRAM 移除多个 segment；MemoryPolicy 只选择起始 leaf，向上展开
和实际状态修改由 Simulator 完成。

## 7. StoragePolicy 介入点

StoragePolicy 是一个完整的 SSD 管理算法，不拆成独立 placement、eviction 和 timer policy。

| 接口 | 触发位置 | Policy 决定什么 |
| --- | --- | --- |
| `background_schedule()` | 初始化 | 虚拟后台 tick 周期，0 表示关闭 |
| `on_request_begin()` | 每条请求、逐 block 之前 | 更新 session/gap 等请求级状态 |
| `place_dump()` | MemoryPolicy 产生有效 dump | 目标 SLC/TLC 和 tier-local stream |
| `capacity_limit_blocks()` | dump/migration 写入前 | 当前 policy 允许使用的逻辑容量 |
| `reclaim_for()` | 目标 tier 容量不足 | 返回一个 `TrimIntent` 或 `RelocateIntent` |
| `on_storage_access()` | storage hit 后 | 是否执行访问触发的 relocation |
| `next_background_action()` | 每个虚拟后台 tick | 返回一个 idle trim 或后台 relocation |
| `on_commit()` | Simulator 完成一次存储状态修改后 | 更新 LRU、first/last time、program bytes 等状态 |
| `stats()` | 生成 metrics | 导出 policy 派生指标 |

### 7.1 Dump placement 与 admission

对 `write_nodes` 非空的 segment，流程是：

```text
Memory DumpContext
  -> StoragePolicy::place_dump()
       returns Placement(tier, stream_id)
  -> StoragePolicy::capacity_limit_blocks()
  -> 容量不足时循环 StoragePolicy::reclaim_for()
  -> Simulator 执行 Trim/Relocate intent
  -> 目标容量足够后按 block 分配 pool-local address
  -> 发出 MEMORY_DUMP WRITE
  -> 更新节点 SSD location、metrics
  -> StoragePolicy::on_commit(DumpWriteCommitted)
```

当前请求的 protected prefix 和正在 dump/migrate 的 segment 都不会作为本次容量回收 victim。若
policy 无法给出可执行 victim，admission 返回失败，不会部分写入该 segment。

### 7.2 同步 capacity reclaim

容量检查不是简单水位判断。只要：

```text
used_blocks + required_blocks > policy_capacity_limit
```

Simulator 就反复调用 `reclaim_for()`，每次执行一个 segment 级动作，直到空间足够或没有合法
victim：

- `TrimIntent`：对目标 tier 中该 segment 的 resident blocks 发出 `TRIM` 并释放地址；
- `RelocateIntent`：确保 destination 容量后执行 `READ -> WRITE -> TRIM`。

StoragePolicy 选择 victim 和动作；Simulator 负责原子执行、依赖、地址和状态变更。

### 7.3 Relocation 的降低

Relocation 是 policy intent，不是 MQSim opcode。Simulator 为 segment 中每个 source-tier block
展开：

```text
READ(source)
  -> WRITE(destination)
       -> TRIM(source)
```

三个事件共享 `move_id`，`WRITE` 依赖对应 `READ`，`TRIM` 依赖对应 `WRITE`。destination WRITE
成功后节点立即切换到新的唯一 SSD location；最后释放 source address。access migration 可复用
当前 storage-hit READ，capacity/background migration 产生显式 source READ。

### 7.4 虚拟后台维护

每个后台 tick 中，Simulator 重复调用 `next_background_action()` 并逐个执行，直到 policy 返回空
或一个动作无法完成。当前只有 `adaptive_endurance` 设置非零周期并返回后台动作：

- 从 SLC/TLC storage leaf 中选择达到动态 idle threshold 的最老 segment，执行 idle `TRIM`；
- 选择达到动态 promotion age 的老 SLC segment，relocate 到 TLC；
- idle 和 promotion 同时到期时，执行到期时间更早的动作。

`finish()` 会把虚拟时间推进到 `simulation_end_ns`；未配置时只推进到最后一条请求的 timestamp。
因此最后一条请求之后的后台维护必须通过显式 simulation end 才能发生。

## 8. 当前 Policy 功能

### 8.1 MemoryPolicy

| `kind` | admission | victim/action |
| --- | --- | --- |
| `baseline_lru` | 由 `admit_storage_hits` 控制 | 最老 memory leaf segment，固定 `Dump` |

### 8.2 StoragePolicy

| `kind` | Dump placement/stream | Capacity reclaim | Access/后台动作 |
| --- | --- | --- | --- |
| `baseline_fixed_lru` | 固定 tier、固定 stream | SLC leaf relocate 到 TLC；TLC leaf Trim | 无 |
| `baseline_ratio_lru` | 按累计 SLC program-byte ratio 选 tier，stream round-robin | SLC leaf relocate 到 TLC；TLC leaf Trim | 无 |
| `wear_share_round_robin` | 按累计 program-byte share 选 tier，stream round-robin | 在受压 tier Trim LRU leaf | 无 |
| `wear_share_affinity` | 按累计 program-byte share 选 tier，affinity hash stream | 在受压 tier Trim LRU leaf | 无 |
| `adaptive_endurance` | 按 SLC/TLC 容量与 erase budget 的耐久份额动态 placement，affinity hash stream | 在受压 tier Trim LRU leaf | SLC access migration、idle Trim、后台 SLC→TLC relocation |

所有 storage policy 共享同一种 commit 驱动的基础状态：各 block 的 first/last storage time、所在
tier、累计 program bytes 和已提交 write segment 数。`baseline_fixed_lru` 与
`baseline_ratio_lru` 使用完整 tier capacity；wear-share 和 adaptive policy 使用
`logical_fill_fraction` 后的 policy capacity。

## 9. 剪枝与生命周期结束

DRAM 移除或 SSD Trim 后，Simulator 尝试从相关节点向 parent 递归剪枝。节点只有同时满足以下条件
才从 RadixTree 删除：

```text
!in_memory && !on_storage && child_count == 0
```

正在处理的 active node 不会被剪枝。删除节点后，Simulator 发送 `NodePruned` commit，让
StoragePolicy 清理派生状态。以后相同 `hash_id` 再次出现时，会以相同逻辑 NodeId 开始一个新的冷
生命周期。

## 10. Canonical trace

只有真实 SSD 语义动作进入 `simulation_trace.csv`。schema v4 为：

```text
sequence,timestamp_ns,request_id,access_sequence,operation,storage_tier,
stream_id,offset_bytes,length_bytes,node_id,hash_id,reason,move_id,
depends_on_sequence
```

operation 只有：

- `READ`：storage hit 或 relocation source read；
- `WRITE`：Memory Dump 或 relocation destination write；
- `TRIM`：capacity/idle eviction 或 relocation source trim。

reason 区分 `STORAGE_HIT`、`MEMORY_DUMP`、`CAPACITY_EVICTION`、`IDLE_EVICTION`、
`ACCESS_MIGRATION` 和 `BACKGROUND_MIGRATION`。offset 是 tier 内的 pool-local byte address；
stream id 也是 tier-local。DRAM hit、global miss、DRAM Drop 本身不产生 trace I/O。

## 11. Metrics

`simulation_metrics.json` 是 DWPDSim 逻辑层结果，主要包括：

- `accesses`：request/block 数、DRAM/SLC/TLC hit、global miss 和命中率；
- `memory`：最终/峰值驻留、淘汰 segment/block、已有 storage copy、dump/drop；
- `dumps`：dump request、admitted 和 rejected 的 segment/block/byte；
- `storage.slc/tlc`：live/peak bytes、READ/WRITE/TRIM、dump-only host write 和 stream write；
- `placement`：成功 dump 到 SLC/TLC 的 segment/block/byte；
- `foreground_capacity_evictions`：同步容量回收；
- `background`：tick 和 idle eviction；
- `migrations`、`relocation`：三类迁移及其 READ/WRITE/TRIM 构成；
- `algorithm`：program bytes、adaptive gap/q95/idle threshold；
- `errors`：no space、protected victim exhaustion、admission rejection；
- `tree`、`trace`：节点生命周期和 canonical event 数。

`memory.dump_*` 在 admission 前计数，可能包含 rejected dump；成功下盘量应使用
`dumps.admitted`。`memory.drop_*` 同时包含 policy 主动 Drop 和 dump admission 失败后的 Drop。

## 12. MQSim 回放

converter 同时读取 canonical trace、DWPDSim metrics 和 SSD XML：

1. 校验 trace schema、event 数、block/capacity 对齐和 SLC/TLC pool 配置；
2. 将 `(tier, tier-local stream)` 映射为固定 MQSim flow；
3. 将 byte offset 转为 512-byte sector LBA；
4. 超过 65535 sectors 的 semantic I/O 切成多个 command；
5. 保留 relocation dependency，并补充同地址 mutation/read hazard；
6. 生成 `flow-*.trace`、`commands.csv`、`workload.xml` 和 `manifest.json`；
7. 一次启动 MQSim 回放全部 SLC/TLC flow；
8. 解析原始 XML，生成包含 flow/pool/channel、host/NAND DWPD、WAF 和最大 block PE/day 的
   `summary.json`。

DWPDSim `storage.<tier>.host_write_bytes` 只统计 Memory Dump。MQSim pool `Host_Write_Bytes`
包含所有 host-visible WRITE，包括 relocation destination WRITE，二者不能作为同一口径比较。

## 13. 代码导航

| 模块 | 位置 | 作用 |
| --- | --- | --- |
| Python API/config | `src/dwpdsim/` | 配置、请求对象、逐请求/批量 facade、metrics 输出 |
| Python/C++ binding | `cpp/src/bindings.cpp` | 参数校验、policy 构造、批量 buffer ABI、stats 序列化 |
| Simulator | `cpp/src/simulator.cpp` | 请求/tick 调度、状态变更、intent 执行、trace 和 metrics 协调 |
| RadixTree | `cpp/src/radix_tree.cpp` | 全局拓扑、segment 解析、访问统计和节点生命周期 |
| StorageState | `cpp/src/storage.cpp` | SLC/TLC 逻辑容量和 pool-local 地址分配 |
| MemoryPolicy | `cpp/include/dwpdsim/policies/memory_policy.hpp` | DRAM admission、victim 和 Dump/Drop 接口 |
| StoragePolicy | `cpp/include/dwpdsim/policies/storage_policy.hpp` | placement/reclaim/access/background/commit 接口 |
| Policy 实现 | `cpp/src/policies/` | baseline、wear-share 和 adaptive-endurance 算法 |
| TraceWriter | `cpp/src/trace_writer.cpp` | canonical schema v4 输出 |
| MQSim converter | `src/dwpdsim/mqsim.py` | flow/workload 生成、MQSim 启动和结果解析 |
| 完整示例 | `example/run_pipeline.py` | dotenv 驱动的 DWPDSim→MQSim pipeline |
