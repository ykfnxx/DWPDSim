# DWPDSim

DWPDSim 回放 KV cache block 请求，在一棵公共 RadixTree 上模拟内存、SLC 和 TLC 的驻留、
placement、淘汰与迁移。C++17 core 是逻辑状态、pool-local 地址、虚拟时间、trace 和 metrics
的唯一所有者；policy 只读取视图并返回决策。vNext 是破坏性接口，设计契约见
[`.design/vnext-policy-refactor.md`](.design/vnext-policy-refactor.md)。

## 行为语义

每条请求携带全局纳秒时间、唯一 request id、用于 stream placement/session gap 的 affinity id，
以及完整有序的 hash path。`hash_id` 同时是全局唯一 `NodeId`，parent link 只表达前缀拓扑。

- memory hit 不产生 I/O；
- storage hit 产生 READ，MemoryPolicy 决定是否提升到内存；
- global miss 代表计算出新 block，并加入内存，不产生 READ；
- 内存按 segment LRU 选择 leaf segment；`Drop` 只剪枝该 leaf segment，`Dump` 才以 segment
  为单位向 parent 贪婪，已写盘 segment 释放内存后继续向上，在首个含未写盘 block 的 segment
  写盘并停止；
- StoragePolicy 统一决定 Dump placement、同步 capacity reclaim、access migration 和后台维护；
- relocation 是管理意图，不是设备 opcode。Simulator 将每个 block 展开为
  `READ(source) -> WRITE(destination) -> TRIM(source)`；access migration 复用本次 storage-hit
  READ，后台 migration 显式产生 READ；
- adaptive-endurance policy 的后台 tick 独立于前台请求运行。相同 timestamp 的 tick 先执行，`finish()` 会将
  虚拟时间推进至配置中冻结的 `simulation_end_ns`；Python `finish()` 不另接收终点参数。

DWPDSim 不模拟 SSD 内部 GC、NAND latency、擦除或物理写放大；这些由下游 MQSim 计算。

## 安装与验证

需要 Python 3.11+、CMake 3.18+ 和 C++17 编译器：

```bash
python3 -m pip install -e .
python3 -m pip install -e '.[dev]'
ruff check .
pytest
```

## 基本用法

```python
from dwpdsim import (
    DWPDSimulator,
    MemoryConfig,
    Request,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)

MIB = 1024 * 1024
BLOCK_SIZE = 4096
config = SimulationConfig(
    block_size_bytes=BLOCK_SIZE,
    memory=MemoryConfig(capacity_bytes=2 * BLOCK_SIZE),
    slc=StorageTierConfig(capacity_bytes=MIB, stream_count=2),
    tlc=StorageTierConfig(capacity_bytes=MIB, stream_count=2),
    storage_policy=StoragePolicyConfig(
        kind="baseline_fixed_lru",
        fixed_tier="tlc",
        fixed_stream_id=0,
    ),
)

with DWPDSimulator(config, "simulation_trace.csv") as simulator:
    simulator.run(
        [
            Request(0, 1, 10, [1, 2, 3]),
            Request(1_000_000_000, 2, 10, [1, 2, 4]),
            Request(2_000_000_000, 3, 20, [5, 6]),
        ]
    )

simulator.write_stats("simulation_metrics.json")
```

`Request` 的位置参数依次是 `timestamp_ns, request_id, affinity_id, hash_ids`。request id 必须唯一，
timestamp 必须非递减。

## 批量输入

大数据集使用五个连续的 `uint64` buffer。第 `i` 条请求对应
`hash_ids[offsets[i]:offsets[i + 1]]`：

```python
import numpy as np

simulator.process_batch(
    np.asarray([0, 10], dtype=np.uint64),          # timestamps_ns
    np.asarray([100, 101], dtype=np.uint64),       # request_ids
    np.asarray([7, 7], dtype=np.uint64),           # affinity_ids
    np.asarray([0, 3, 5], dtype=np.uint64),        # offsets
    np.asarray([1, 2, 3, 1, 4], dtype=np.uint64), # hash_ids
)
```

批量和逐请求接口产生相同的 tick、决策、trace 和 metrics。

## Policy

顶层接口只有 `MemoryPolicy` 和 `StoragePolicy`：

- `baseline_lru`：memory admission、leaf-first segment LRU，并返回向 parent segment 贪婪的
  `Dump`；
- `baseline_fixed_lru`：固定 tier/stream placement，leaf-LRU capacity reclaim；
- `baseline_ratio_lru`：按 SLC write ratio placement，pool 内 round-robin stream；
- `wear_share_round_robin`：wear-share tier placement 与 pool 内 round-robin stream；
- `wear_share_affinity`：wear-share tier placement 与 affinity hash stream；
- `adaptive_endurance`：endurance-weighted placement、session gap/q95、自适应 promotion、access
  migration、周期 idle eviction 和后台 migration。

参考目录中的三种算法在当前代码中使用按决策机制命名的 policy：

| 参考算法 | 当前 `StoragePolicyConfig.kind` | C++ policy | 保留的决策 |
| --- | --- | --- | --- |
| RR / `rr_wear_sb` | `wear_share_round_robin` | `WearShareRoundRobinStoragePolicy` | 按目标写入份额选择 SLC/TLC，pool 内 round-robin stream |
| Algorithm1 / `session_wear_sb` | `wear_share_affinity` | `WearShareAffinityStoragePolicy` | 按目标写入份额选择 SLC/TLC，用 affinity 稳定散列到 stream |
| Algorithm2 / `tiered2` | `adaptive_endurance` | `AdaptiveEnduranceStoragePolicy` | endurance-weighted placement、session gap/q95、occupancy pressure、动态 promotion、后台 migration 和 idle eviction |

这是算法决策的对应关系，不是旧名兼容层；参考算法名不能作为当前 `kind`
传入。三种 policy 都只在 MemoryPolicy 产生 Dump 时决定初始 placement，global miss
只先进入内存。`adaptive_endurance` 在 Dump 和 access migration 中使用请求 affinity；
后台 migration 没有请求上下文，使用 segment endpoint 选择目标 stream。

所有 storage 决策共用一个 policy state，并只通过 commit notification 更新。实现位于
`cpp/include/dwpdsim/policies/` 和 `cpp/src/policies/`；Simulator 保持 RadixTree、StorageState
和 LBA allocator 的唯一写权限。
后台 tick、segment 展开和 `READ -> WRITE -> TRIM` 降低由 Simulator 统一执行；MQSim
只模拟已决定的物理 I/O，不运行上述 policy。

## Canonical trace 与 metrics

trace schema version 4 固定为 14 列：

```text
sequence,timestamp_ns,request_id,access_sequence,operation,storage_tier,stream_id,offset_bytes,length_bytes,node_id,hash_id,reason,move_id,depends_on_sequence
```

operation 只有 `READ`、`WRITE`、`TRIM`。`sequence` 是全局 semantic I/O 编号；offset 是原始
pool-local byte address；stream id 是 tier-local。relocation 的同一 segment 共用 `move_id`，
每个 block 各自形成 READ、WRITE、TRIM completion chain。非 relocation I/O 不设置 dependency。

`metrics.json` 的主要口径包括：request/block accesses 与 hit rate、Dump admission/rejection、
前台 capacity eviction、后台 tick/idle eviction、三类 migration、relocation source read/
destination write/source trim、SLC/TLC live/peak/program/host-write bytes、每 stream write bytes、
adaptive-endurance gap/q95/idle threshold、placement 和错误计数。DWPDSim 的
`host_write_bytes` 只统计
Memory Dump；relocation destination WRITE 只进入 program bytes。

## MQSim pipeline

完整的 workload、DWPDSim policy、SSD XML 和运行配置见
[`docs/mqsim-pipeline.md`](docs/mqsim-pipeline.md)。
可直接复制 dotenv 模板并运行完整示例：

```bash
cp example/.env.example example/.env
.venv/bin/python example/run_pipeline.py
```

配套 MQSim vNext 在一次运行中回放全部 SLC/TLC flow。先编译同级 MQSim，然后运行：

```bash
make -C ../MQSim
python3 scripts/mqsim_pipeline.py simulation_trace.csv simulation_metrics.json \
  --mqsim-binary ../MQSim/MQSim \
  --ssd-config example/mqsim/ssdconfig.xml \
  --output build/mqsim-run
```

converter 的稳定契约是：

- 只接受 schema v4 的精确 header，并在完整转换时核对 metrics `trace.events`；
- 只接受一个同时包含精确 `slc`/`tlc` pool 的 SSD XML；pool logical capacity 必须与 DWPDSim
  metrics 一致，measurement window 使用同一绝对纳秒时间轴；设备必须使用 NVMe、FLASH、
  PAGE_LEVEL mapping，关闭 preconditioning，且两个 pool 分别引用 SLC/TLC media profile；
- 固定生成全部配置 stream：SLC flow 先于 TLC flow，空 stream 也生成空 trace；MQSim V1 的
  NVMe queue ABI 限制总 flow 数不超过 8；
- 保留 tier-local stream 与 pool-local LBA，不按 NodeId 或 active stream 重映射；
- 为每个实际 command 分配全局连续 id。超过 65535 sectors 的 semantic I/O 按地址切分，
  `commands.csv` 保存 command id 到 source sequence/chunk 的映射；
- `DWPDSIM_DEPENDENCY_V1` 的最后一列是 `depends_on_request_ids`，使用 `-1` 或逗号分隔的多个
  command id。converter 合并 relocation/chunk dependency 与同一 `(tier, stream, range)` 的
  mutation/read hazard，不串行并发 READ；
- `manifest.json` 冻结 trace/metrics/config path、SSD 内容 hash、measurement window、固定 flow
  映射及逐 pool/flow command 和 byte totals；
- `summary.json` 按显式 Flow_ID、Pool_ID、Channel ID 消费 MQSim result ABI v1，核对配置 hash、
  window、容量、请求和字节数，并计算 measurement host DWPD、NAND DWPD、WAF 与最大 block
  PE/day。零分母写 `null`。

MQSim 的 pool `Host_Write_Bytes` 包含 Memory Dump 和 relocation destination WRITE；它与
DWPDSim dump-only `storage.<tier>.host_write_bytes` 是两个独立口径，summary 同时保留两者。
`--event-limit N` 只用于转换/回放前 N 条 semantic I/O 的快速检查；manifest 仍记录完整输入行数。
