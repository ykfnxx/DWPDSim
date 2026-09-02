# DWPDSim

DWPDSim 回放 vLLM KVConnector 收到的 KV cache block 请求，模拟 block 在内存、SLC
和 TLC 中的驻留与淘汰，并输出缓存统计和通用 SSD I/O trace。

模拟核心使用 C++17，Python 只负责数据集适配、批量输入和结果处理。完整设计见
[`.design/rewrite-design.md`](.design/rewrite-design.md)。

## 行为语义

输入中的一条请求为有序的 uint64 hash 序列：

```python
Request(timestamp=100, hash_ids=[11, 22, 33])
```

序列会被加入以 `(parent_node_id, hash_id)` 为节点身份的前缀树。对每个节点：

- 内存存在：memory hit，不产生 SSD I/O；
- 内存不存在、SLC/TLC 存在：产生 READ，MemoryPolicy 决定是否提升到内存；
- 三处均不存在：global miss，认为计算完成并强制加入内存；
- 内存淘汰时可丢弃，也可由 WritePlacementPolicy 选择介质和 stream 后写盘；
- 目标介质满时，StorageEvictionPolicy 选择 victim 并依次产生 TRIM、WRITE。

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

- MemoryPolicy：LRU，可配置 storage hit 是否提升，以及 victim 使用 `drop` 或 `persist`；
- WritePlacementPolicy：固定介质/stream，或按比例分配 SLC/TLC 并轮转 stream；
- StorageEvictionPolicy：SLC/TLC 各自独立的 LRU。

三类 policy 都是独立的 C++ 抽象接口。新增算法时直接实现对应接口并在 pybind11 构造入口
注册，不使用逐访问的 Python callback。

## 输出指标

`metrics.json` 包含：

- Request 和 block access 数；
- memory/SLC/TLC hit、global miss 及命中率；
- promote、bypass、内存淘汰、drop、persist；
- SLC/TLC 的 READ、WRITE、TRIM 数量和字节数；
- 每个 stream 的写入量；
- 当前及峰值驻留量、重复副本数和前缀树节点数。

写放大、GC、擦除和 DWPD 不属于核心模拟结果，可在下游 SSD 模拟或分析中计算。
