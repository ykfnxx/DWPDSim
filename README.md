# DWPDSim

DWPDSim 是一个块级 DRAM + SSD 模拟器：manager 持有并校验全部状态，policy
只做选择。每个 `Query` 的 `hash_ids` 按原顺序逐个执行固定流程：先查 DRAM，
再查并列的 SLC/TLC；两处都不存在时视为 global miss，并强制把新 block 插入
DRAM。SLC 与 TLC 不是上下级关系。

`timestamp` 的单位固定为秒。相同 timestamp 的 Query 保持输入顺序；同一 Query
中的重复 block 不去重，因此前一个位置造成的状态变化会影响后一个位置。

## 安装

需要 Python 3.11 或更高版本：

```bash
python -m pip install -e .
```

## 基本用法

公开入口从 `dwpdsim` 顶层导入：

```python
from dwpdsim import DWPDSimulator, Medium, Query, SimulationConfig, SSDConfig

config = SimulationConfig(
    block_size_bytes=4096,
    dram_capacity_bytes=2 * 4096,
    slc=SSDConfig(
        capacity_bytes=64 * 1024,
        chunk_size_bytes=16 * 1024,
        stream_count=2,
        gc_reserve_chunks=1,
    ),
    tlc=SSDConfig(
        capacity_bytes=128 * 1024,
        chunk_size_bytes=16 * 1024,
        stream_count=2,
        gc_reserve_chunks=1,
    ),
)

simulator = DWPDSimulator.from_config(config)

# 显式构造初始持久层状态；seed 不计模拟写入。
simulator.storage.seed(1, Medium.SLC, stream_id=0)
simulator.storage.seed(4, Medium.TLC, stream_id=1)

stats = simulator.run(
    [
        Query(timestamp=0.0, hash_ids=(1, 2, 1)),
        Query(
            timestamp=3600.0,
            hash_ids=(3, 4, 2),
            other_info={"request_id": "request-2"},
        ),
    ]
)
simulator.write_stats("simulation_stats.json")
print(stats["accesses"])
```

`other_info` 可省略。未通过 `storage.seed()` 初始化、且运行时也不在 DRAM 或
SSD 中的 block，会按 global miss 直接创建到 DRAM；初始 block 不会被隐式假定
存在。

完整示例：

```bash
python example/basic_simulation.py
```

## 配置约束

每个 SSD 的 chunk 数为：

```text
chunk_count = capacity_bytes // chunk_size_bytes
```

配置必须满足：

- `dram_capacity_bytes` 能被 `block_size_bytes` 整除；
- SSD `capacity_bytes` 能被 `chunk_size_bytes` 整除；
- `chunk_size_bytes` 能被 `block_size_bytes` 整除；
- `chunk_count >= stream_count + gc_reserve_chunks`；
- `gc_reserve_chunks >= 1`。

reserve chunk 留给 GC，普通写入不能占用。每个 stream 最多有一个 active chunk；
active chunk 写满后变为 sealed。删除 block 会使旧 slot 失效，并立即触发整块擦除
或同介质、同 stream 的压缩 GC；GC 搬移不计 logical write。

`capacity_bytes` 包含 reserve chunk。manager 可放普通数据的容量会扣掉 reserve，
但统计文件中的介质容量和 DWPD 分母使用完整的 `capacity_bytes`。

## 固定访问与写回语义

对每个 `hash_id`：

1. DRAM hit：访问结束。
2. DRAM miss：StorageManager 只查询一次，结果为 SLC hit、TLC hit 或不存在。
3. storage hit：由 DramPolicy 决定是否放入 DRAM。
4. global miss：跳过 admission 决策，强制放入 DRAM。
5. DRAM 满时先选择 victim。victim 在 SLC/TLC 中已有副本则直接移除；否则由
   PlacementPolicy 决定写入某个介质和 stream，或明确丢弃。

持久层对每个 block 只允许一个位置，即 SLC 或 TLC；DRAM 可以同时保存缓存副本。
介质迁移仅由显式调用
`storage.transfer(block_id, target_medium, target_stream)` 发起。

## 四类 policy

policy 位于四个独立目录，返回决策但不直接修改 manager：

- `src/dwpdsim/policies/dram/`：storage hit admission 与 DRAM victim；
- `src/dwpdsim/policies/placement/`：dirty DRAM victim 的 SLC/TLC、stream 或 DROP；
- `src/dwpdsim/policies/storage_eviction/`：目标 SSD 空间不足时选择持久层 victim；
- `src/dwpdsim/policies/gc/`：失效 chunk 的有效 block 搬移顺序。

内置实现可从 `dwpdsim.policies` 导入，并通过 `DWPDSimulator.from_config()` 的
`dram_policy`、`placement_policy`、`storage_eviction_policy`、`slc_gc_policy`
和 `tlc_gc_policy` 参数注入。manager 会在修改状态前校验 policy 返回值。

## 统计与 DWPD 分析

`run()` 返回与 `write_stats()` 相同结构的原始统计。JSON 包含：

- `time`：起止 timestamp 和 `duration_seconds`；
- `configuration`：block size 与 DRAM/SLC/TLC 容量；
- `accesses`：访问数、四类结果及命中率，其中 `storage_hit_rate` 的分母是
  DRAM miss；
- `created`：global miss 创建的 block 数和字节数；
- `writes_from_dram`：DRAM 到 SLC/TLC 的逻辑写入；
- `transfers`：SLC→TLC 与 TLC→SLC 的显式迁移；
- `erases`：`direct` 表示删除后已无有效 block、可直接擦除，`non_full` 表示仍有
  有效 block、需要先重排再擦除；两者最终都擦除整个 chunk；
- `stream_writes`：各 stream 的逻辑写入，不含 GC 搬移。

实际写放大作为外部参数交给分析脚本，不属于模拟配置：

```bash
python scripts/analyze.py simulation_stats.json \
  --slc-wa 1.2 \
  --tlc-wa 2.0 \
  --output endurance.json
```

`--slc-wa` 和 `--tlc-wa` 必须不小于 1，且模拟时间必须大于 0 秒。输出包含
SLC/TLC 逻辑写入、估算物理写入、各介质 DWPD 和 system-equivalent DWPD。

完整 SwissAI Qwen3-80B Thinking trace 的固定 TLC 控制组见
[`benchmark/swissai_baseline.py`](benchmark/swissai_baseline.py) 和
[`benchmark/results/qwen380b_thinking_baseline.md`](benchmark/results/qwen380b_thinking_baseline.md)。
这组 baseline 配置 `block_size_bytes=4096`，即每个 block 按 4 KiB 计量。
4 KiB 只是归一化配置，不是从 trace 推导出的真实 KV block 大小；使用结果估算实际
写入字节数和 DWPD 时，应替换为目标模型与部署方式对应的 block 大小。

## License

DWPDSim 使用 [MIT License](LICENSE)。
