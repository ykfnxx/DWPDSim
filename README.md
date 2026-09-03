# DWPDSim

DWPDSim 回放 vLLM KVConnector 收到的 KV cache block 请求，模拟 block 在内存、SLC
和 TLC 中的驻留与淘汰，并输出缓存统计和通用 SSD I/O trace。

模拟核心使用 C++17，Python 只负责数据集适配、批量输入和结果处理。完整设计见
[`.design/rewrite-design.md`](.design/rewrite-design.md) 和
[`.design/segment-policy-design.md`](.design/segment-policy-design.md)。

## 行为语义

输入中的一条请求为有序的 uint64 hash 序列：

```python
Request(timestamp=100, hash_ids=[11, 22, 33])
```

每个输入 `hash_id` 是全局唯一逻辑 block 标识，并直接作为 `NodeId`；parent 只表达前缀
拓扑，不参与节点身份。对每个节点：

- 内存存在：memory hit，不产生 SSD I/O；
- 内存不存在、SLC/TLC 存在：产生 READ，MemoryPolicy 决定是否提升到内存；
- 三处均不存在：global miss，认为计算完成并强制加入内存；
- 内存淘汰以 radix segment 为逻辑批次；segment 内每个 block 独立决定复用盘上副本、
  丢弃或由 WritePlacementPolicy 选择介质和 stream 后写盘；
- 目标介质满时，StorageEvictionPolicy 选择 segment，对该介质子集逐 block 产生 TRIM，
  再执行当前 WRITE；
- block 在所有介质消失且没有 child 后从树中删除，访问统计随之丢弃。

SLC 与 TLC 是并列介质。DWPDSim 不模拟 SSD 内部 GC、擦除、数据搬移或设备延迟。

## 安装

需要 Python 3.11+、C++17 编译器和 CMake：

```bash
python -m pip install -e .
```

开发环境：

```bash
uv sync --extra dev
uv run pytest
```

## 基本用法

```python
from dwpdsim import (
    DWPDSimulator,
    MediumConfig,
    PlacementPolicyConfig,
    Request,
    SimulationConfig,
)

MIB = 1024 * 1024
config = SimulationConfig(
    block_size_bytes=8 * MIB,
    memory_capacity_bytes=2 * 8 * MIB,
    slc=MediumConfig(capacity_bytes=64 * 8 * MIB, stream_count=2),
    tlc=MediumConfig(capacity_bytes=128 * 8 * MIB, stream_count=4),
    timestamp_unit="us",
)

with DWPDSimulator(
    config,
    "trace.csv",
    placement_policy=PlacementPolicyConfig(
        kind="fixed",
        fixed_medium="tlc",
        fixed_stream_id=0,
    ),
) as simulator:
    simulator.run(
        [
            Request(0, [1, 2, 3]),
            Request(10, [1, 2, 4]),
            Request(20, [5, 6]),
        ]
    )

simulator.write_stats("metrics.json")
```

`trace.csv` 包含 READ、WRITE、TRIM，以及对应的介质、stream、逻辑地址、树节点和 hash。
WRITE 必定带有 stream 信息。

## 批量输入

大数据集应使用三个连续的 uint64 buffer，避免逐 block 进入 Python：

```python
import numpy as np

timestamps = np.asarray([0, 10], dtype=np.uint64)
offsets = np.asarray([0, 3, 5], dtype=np.uint64)
hash_ids = np.asarray([1, 2, 3, 1, 4], dtype=np.uint64)

simulator.process_batch(timestamps, offsets, hash_ids)
```

第 `i` 条请求对应 `hash_ids[offsets[i]:offsets[i + 1]]`。C++ 处理 batch 时释放 GIL。

## 内置 policy

- MemoryPolicy：LRU，可配置 storage hit 是否提升，以及 segment 中无盘副本 block 使用
  `drop` 或 `persist`；
- WritePlacementPolicy：固定介质/stream，或按比例分配 SLC/TLC 并轮转 stream；
- StorageEvictionPolicy：SLC/TLC 各自独立的 LRU segment 选择。

三类 policy 都是独立的 C++ 抽象接口，决策接口接收只读 `RadixTree`，并可通过节点创建、
删除和访问完成通知维护派生状态。新增算法时直接实现对应接口并在 pybind11 构造入口注册，
不使用逐访问的 Python callback。

## 输出指标

`metrics.json` 包含：

- Request 和 block access 数；
- memory/SLC/TLC hit、global miss 及命中率；
- promote、bypass、segment/block 淘汰、drop、persist；
- SLC/TLC 的 READ、WRITE、TRIM 数量和字节数；
- 每个 stream 的写入量；
- 当前及峰值驻留量、重复副本数、当前树节点数以及节点创建/删除数。

写放大、GC、擦除和 DWPD 不属于核心模拟结果，可在下游 SSD 模拟或分析中计算。

## MQSim pipeline

仓库中的适配器可将通用 CSV trace 按介质和 stream 转换为本地 MQSim trace，分别运行
SLC 与 TLC 仿真，并将关键结果汇总为 JSON。先编译同级目录中的 MQSim：

```bash
make -C ../MQSim
```

然后使用 DWPDSim 生成的 trace 和 metrics：

```bash
uv run python scripts/mqsim_pipeline.py trace.csv metrics.json \
  --mqsim-binary ../MQSim/MQSim \
  --slc-config example/mqsim/ssdconfig-slc.xml \
  --tlc-config example/mqsim/ssdconfig-tlc.xml \
  --output build/mqsim-run \
  --event-limit 100
```

`--event-limit` 仅用于快速验证；去掉后会转换并执行完整 trace。输出目录包含：

- `manifest.json`：输入、时间单位、介质、stream 映射和所需容量；
- `slc/`、`tlc/`：每个活跃 stream 的 trace、生成的 workload 和 MQSim XML 结果；
- `summary.json`：各 flow 的请求统计和 FTL 统计。

转换器将 `s`、`ms`、`us` 或 `ns` 时间戳换算为纳秒，并让每种介质从自己的首个事件
开始计时。一个 DWPDSim stream 对应一个 MQSim flow；同一介质最多支持 8 个活跃
stream。LBA 会按 stream 紧凑重映射，WRITE 分配地址，TRIM 释放地址，后续 WRITE 可
复用该地址。这样生成的地址与 MQSim 对 flow 的逻辑地址分区一致，不保留通用 trace
中的原始 `offset_bytes`。

示例 SLC/TLC 配置容量较小并使用 ideal mapping table，只用于验证 pipeline，不代表
经过校准的 SSD。替换配置时需使用 `PAGE_LEVEL` 地址映射；生成的 workload 会关闭
device data cache，以满足当前本地 MQSim 的 TRIM 语义。Python 代码也可以直接调用
`dwpdsim.mqsim.convert_trace` 和 `dwpdsim.mqsim.run_mqsim`。
