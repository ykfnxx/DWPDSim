# DWPDSim 到 MQSim 的运行配置

整条 pipeline 分为两段：DWPDSim 回放 KV cache 请求并输出逻辑 I/O，converter 再将这些
I/O 展开成 MQSim 的多 flow workload，最后由 MQSim 计算设备层时延、写放大和磨损指标。

```text
workload adapter
  -> Request(timestamp_ns, request_id, affinity_id, hash_ids)
  -> DWPDSim SimulationConfig
  -> simulation_trace.csv + simulation_metrics.json
  -> mqsim_pipeline.py + SSD XML
  -> flow-*.trace + workload.xml
  -> MQSim
  -> summary.json
```

需要人工准备的输入只有三类：workload adapter、DWPDSim 配置和 MQSim SSD XML。
`flow-*.trace`、`workload.xml`、依赖关系和结果汇总均由 converter 生成。

## 环境准备

在 DWPDSim 仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
make -C ../MQSim
```

后续命令假定当前目录为 DWPDSim 仓库根目录，MQSim 与 DWPDSim 是同级目录。

## 1. Workload 输入

每条请求必须转换成：

```python
Request(timestamp_ns, request_id, affinity_id, hash_ids)
```

| 字段 | 契约 |
| --- | --- |
| `timestamp_ns` | 全局纳秒时间，同一轮模拟内非递减；建议以第一条请求为 0 做归一化 |
| `request_id` | 全局唯一 |
| `affinity_id` | 稳定的 session/conversation 身份，用于 affinity stream placement 和 adaptive session gap |
| `hash_ids` | 当前请求的完整、有序 RadixTree 路径 |

`affinity_id=0` 时，adaptive policy 用 `request_id` 代替 session identity 统计 gap，placement
则退化为 segment identity。若每条请求都使用不同的 affinity，session gap/q95 不会表达真实的
会话间隔；评估 `wear_share_affinity` 或 `adaptive_endurance` 时应由 workload adapter 提供稳定的
非零 affinity。

大数据集应使用 `process_batch()`，五个输入都必须是连续的 `uint64` buffer：

```python
simulator.process_batch(
    timestamps_ns,
    request_ids,
    affinity_ids,
    offsets,
    hash_ids,
)
```

第 `i` 条请求的路径为 `hash_ids[offsets[i]:offsets[i + 1]]`。

## 2. DWPDSim 配置

完整配置入口是 `SimulationConfig`：

```python
from dwpdsim import (
    MemoryConfig,
    MemoryPolicyConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)

config = SimulationConfig(
    block_size_bytes=4096,
    memory=MemoryConfig(capacity_bytes=2 * 4096),
    slc=StorageTierConfig(capacity_bytes=1024 * 1024, stream_count=2),
    tlc=StorageTierConfig(capacity_bytes=1024 * 1024, stream_count=2),
    memory_policy=MemoryPolicyConfig(
        kind="baseline_lru",
        admit_storage_hits=True,
    ),
    storage_policy=StoragePolicyConfig(
        kind="baseline_fixed_lru",
        fixed_tier="tlc",
        fixed_stream_id=0,
    ),
    simulation_end_ns=None,
    progress_interval_requests=0,
)
```

### 容量、block 和 stream

| 配置 | 含义 |
| --- | --- |
| `block_size_bytes` | 一个 RadixTree node 对应的逻辑 block 大小 |
| `memory.capacity_bytes` | 内存逻辑容量 |
| `slc.capacity_bytes` / `tlc.capacity_bytes` | 两个 SSD pool 的原始逻辑容量 |
| `slc.stream_count` / `tlc.stream_count` | tier-local stream 数；converter 为每个配置的 stream 生成一个 flow，包括空 flow |
| `simulation_end_ns` | 模拟结束时间；用于在最后一条请求之后继续执行 adaptive 后台 tick |
| `progress_interval_requests` | 每处理多少条请求输出一次进度，0 表示关闭 |

配置必须满足：

- `block_size_bytes > 0`；
- memory、SLC、TLC 容量均至少包含一个 block，并且是 `block_size_bytes` 的整数倍；
- SLC、TLC 的 `stream_count` 都大于 0，二者之和不超过 8；
- 进入 MQSim pipeline 时，`block_size_bytes` 以及两个 storage capacity 还必须按 512 byte 对齐；
- `simulation_end_ns` 不得早于最后一条请求。未设置时，`finish()` 只推进到最后一条请求的
  timestamp，不会额外产生请求结束后的后台 tick。

### Memory policy

当前 memory policy 固定为 `baseline_lru`：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `admit_storage_hits` | `True` | storage hit 后是否将 block 提升到内存 |

`Dump`/`Drop` 是每次 `MemoryPolicy::evict()` 返回的决策，不是独立配置项。当前
`baseline_lru` 总是返回 `Dump`：Simulator 从选中的 leaf segment 开始向 parent segment
贪婪回收，在首个含未写盘 block 的 segment 进入 StoragePolicy placement 并产生 WRITE。
返回 `Drop` 的 memory policy 只剪枝它选中的 leaf segment，不继续处理 parent segment。

### Storage policy

| `kind` | 需要关注的参数 | 行为 |
| --- | --- | --- |
| `baseline_fixed_lru` | `fixed_tier="tlc"`、`fixed_stream_id=0` | 所有 Dump 固定写入一个 tier-local stream |
| `baseline_ratio_lru` | `slc_write_ratio=0.0` | 按 SLC 写入比例选择 tier，pool 内 round-robin stream；比例范围为 `[0, 1]` |
| `wear_share_round_robin` | `slc_host_share`、`logical_fill_fraction` | 按目标写入份额选择 tier，pool 内 round-robin stream |
| `wear_share_affinity` | `slc_host_share`、`logical_fill_fraction` | 按目标写入份额选择 tier，用稳定 affinity hash 选择 stream |
| `adaptive_endurance` | 下表的 adaptive 参数 | endurance-weighted placement、session gap/q95、access migration 和后台维护 |

`fixed_stream_id` 是 `fixed_tier` 内部的 stream id，必须落在该 tier 的范围内。
`slc_host_share=None` 时按 policy 解析为：

- `wear_share_round_robin`: `0.405`；
- `wear_share_affinity`: `0.68`；
- `adaptive_endurance`: `0.8333333333`。

`StoragePolicyConfig` 的完整默认值如下：

| 参数 | 默认值 |
| --- | ---: |
| `fixed_tier` | `"tlc"` |
| `fixed_stream_id` | `0` |
| `slc_write_ratio` | `0.0` |
| `slc_host_share` | `None` |
| `idle_multiplier` | `32.0` |
| `promotion_seconds` | `14400.0` |
| `adaptation_gain` | `2.0` |
| `direct_gain` | `1.0` |
| `slc_soft_utilization` | `0.75` |
| `occupancy_decay` | `8.0` |
| `logical_fill_fraction` | `0.98` |
| `slc_erase_budget` | `120.0` |
| `tlc_erase_budget` | `12.0` |
| `background_period_ns` | `900000000000`（15 分钟） |

`logical_fill_fraction` 是 storage policy 的逻辑使用水位。MQSim XML 中的 pool capacity 仍必须
匹配 `StorageTierConfig.capacity_bytes` 的原始容量，不能乘以该比例。

使用 `adaptive_endurance` 时，若实验需要覆盖最后一条请求之后的 idle eviction 或后台
migration，应显式设置 `simulation_end_ns`。它和 MQSim measurement window 使用同一个以 ns
表示的时间轴。

## 3. MQSim SSD XML

可以复制 `example/mqsim/ssdconfig.xml` 后修改。converter 对 XML 有以下硬性要求：

- `Enabled_Preconditioning=false`；
- `Memory_Type=FLASH`；
- `HostInterface_Type=NVME`；
- `Address_Mapping=PAGE_LEVEL`；
- 恰好存在 `Pool_ID=slc` 和 `Pool_ID=tlc` 两个 pool；
- 每个 pool 的 `Channel_IDs` 非空、内部不重复，两个 pool 的 channel 集合互不相交；
- 两个 pool 分别引用 `Flash_Technology=SLC` 和 `Flash_Technology=TLC` 的 media profile；
- `Logical_Capacity_In_Sectors * 512` 必须精确等于对应的
  `StorageTierConfig.capacity_bytes`；
- `Measurement_Start_Time_Ns >= 0`，且 `Measurement_Start_Time_Ns < Measurement_End_Time_Ns`。

stream 与 channel 不是一一对应关系。DWPDSim 的 stream 会成为 MQSim flow；XML 的 channel
集合定义 pool 使用的物理通道。一次运行的 SLC/TLC flow 总数仍受 8 个 flow 的 ABI 限制。

用于正式对比实验时，还需要确定并在所有 policy 之间保持一致的物理参数，包括：

- channel/chip/die/plane/block/page geometry；
- SLC/TLC read、program、erase latency 和 PE cycle limit；
- over-provisioning、GC threshold 和 GC policy；
- NVMe queue、cache 和调度参数；
- measurement window。

示例 XML 的两个 pool 都只有 1 MiB，并使用很小的 geometry 和 `3/7` 次 PE limit，仅用于冒烟
验证，不代表正式 SSD 配置。

## 4. 运行 pipeline

仓库在 `example/run_pipeline.py` 提供了一条命令跑完 DWPDSim、converter 和 MQSim 的
示例。先复制 dotenv 模板：

```bash
cp example/.env.example example/.env
```

`example/.env` 包含 workload/输出路径、DWPDSim 容量和 stream、memory/storage policy
参数、仿真终点、MQSim binary 和 SSD XML 路径。默认值与
`example/requests.jsonl` 及 `example/mqsim/ssdconfig.xml` 严格匹配。修改完参数后执行：

```bash
.venv/bin/python example/run_pipeline.py
```

输入 JSONL 每行是一条 `Request`：

```json
{"timestamp_ns": 0, "request_id": 1, "affinity_id": 10, "hash_ids": [1, 2, 3]}
```

以 `DWPDSIM_` 为前缀的 shell 环境变量优先于 `.env` 中的同名值，可用于临时覆盖单个参数。

下面是不使用完整示例脚本时的分步调用方式。先运行 workload adapter，得到来自同一次、
已经 `finish()` 的 DWPDSim 模拟的 trace 和 metrics：

```python
with DWPDSimulator(config, "simulation_trace.csv") as simulator:
    simulator.run(requests)

simulator.write_stats("simulation_metrics.json")
```

然后转换并运行 MQSim：

```bash
.venv/bin/python scripts/mqsim_pipeline.py \
  simulation_trace.csv \
  simulation_metrics.json \
  --mqsim-binary ../MQSim/MQSim \
  --ssd-config example/mqsim/ssdconfig.xml \
  --output build/mqsim-run
```

`--event-limit N` 只用于快速调试前 N 条 semantic I/O，不应作为完整实验结果。

### 最小冒烟验证

默认 dotenv、JSONL workload 与 SSD XML 容量一致，可以直接运行：

```bash
cp example/.env.example example/.env
.venv/bin/python example/run_pipeline.py
```

成功后输出目录包含：

| 文件 | 内容 |
| --- | --- |
| `flow-*.trace` | 每个配置 stream 的 MQSim command trace |
| `workload.xml` | 一次回放全部 SLC/TLC flow 的 MQSim workload |
| `commands.csv` | MQSim command 到 DWPDSim semantic event/chunk/dependency 的映射 |
| `manifest.json` | 输入路径、SSD XML hash、容量、时间窗、flow 映射和字节总量 |
| `workload_scenario_1.xml` | MQSim 原始结果 |
| `summary.json` | pool/flow/device 统计以及 host DWPD、NAND DWPD、WAF、最大 block PE/day |

## 5. SwissAI workload 注意事项

`benchmark/swissai_baseline.py` 当前使用：

| 项目 | 配置 |
| --- | ---: |
| block size | 8 MiB |
| memory | 65,536 blocks = 512 GiB |
| SLC | 262,144 blocks = 2 TiB = 4,294,967,296 sectors |
| TLC | 2,097,152 blocks = 16 TiB = 34,359,738,368 sectors |
| stream | SLC 1 + TLC 1 |

因此它不能直接使用两个 pool 都为 1 MiB 的示例 SSD XML。正式回放必须让 XML 中的 logical
capacity 与上述 SLC/TLC 容量精确匹配，并同步设计能够容纳该逻辑容量的物理 geometry；只修改
`Logical_Capacity_In_Sectors` 不能形成有意义的物理 SSD 模型。

该 benchmark 当前还把每条请求的 `request_count` 作为 affinity，适合作为 fixed baseline 输入，
但不适合直接评估 adaptive session gap。要运行 `wear_share_affinity` 或
`adaptive_endurance`，应先让 workload adapter 从原始数据生成稳定的 session/conversation id。

## 运行前检查表

- workload timestamp 已归一化并全局非递减；
- request id 全局唯一，affinity 符合所选 policy 的语义；
- block size、memory/SLC/TLC capacity 是整数 block；
- block size 和 storage capacity 按 512 byte 对齐；
- SLC/TLC stream 总数不超过 8；
- memory policy 的 `Dump`/`Drop` 决策符合实验语义；
- adaptive 实验设置了需要的 `simulation_end_ns`；
- trace 和 metrics 来自同一次完成的模拟；
- XML pool logical capacity 精确匹配 DWPDSim 配置；
- XML measurement window 覆盖需要统计的时间范围；
- 不同 policy 实验固定使用同一份 workload 和物理 SSD 参数。
