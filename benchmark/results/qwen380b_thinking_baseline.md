# Qwen3-80B Thinking DWPDSim baseline

## Baseline 定义

- 数据：`qwen380b_thinking_bucket-reuse.jsonl`
- 数据 SHA-256：`e0f035e0cf1cf99c15d28571f9eb6d1de8d76f637676dbf675a0a4338b1c35e9`
- 整个文件作为一个共享状态的 service 流，从空状态开始。
- 原始 `bucket_ids` 按 `(parent_prefix, bucket_id)` 转为规范化 block ID。
- DRAM 使用 LRU，存储命中直接接纳。
- DRAM 淘汰的无副本 block 固定写入 TLC stream 0。
- 不使用 SLC，不做自动介质转移。
- TLC 容量覆盖完整 working set，因此不触发存储淘汰和 GC。

| 配置 | 数量 | 4 KiB 计量下的容量 |
| --- | ---: | ---: |
| DRAM | 65,536 blocks | 256 MiB |
| SLC | 262,144 blocks | 1 GiB |
| TLC | 2,097,152 blocks | 8 GiB |
| chunk | 1,024 blocks | 4 MiB |
| stream | 每种介质 1 个 | - |
| GC reserve | 每种介质 1 个 chunk | - |

这里的 4 KiB 是归一化计量单位，不是从 trace 推导出的 Qwen3-80B 真实 KV block
大小。trace 不包含 KV dtype、模型层数、并行切分或每个 block 的物理字节数。
命中次数和 block 写入次数不受这个计量单位影响，字节数和 DWPD 需要结合实际产品
配置重新解释。

## 全量结果

| 指标 | 结果 |
| --- | ---: |
| 运行耗时 | 410.295 秒 |
| trace 时长 | 1,343,446.231 秒（15.549 天） |
| Query | 42,121 |
| block 访问 | 47,935,909 |
| 唯一规范化 block | 1,655,391 |
| DRAM hit | 46,174,883 |
| TLC hit | 105,635 |
| global miss | 1,655,391 |
| DRAM hit rate | 96.3263% |
| storage hit rate | 5.9985%（分母为 DRAM miss） |
| total hit rate | 96.5467% |
| DRAM -> TLC 写入 | 1,604,765 blocks |
| DRAM -> SLC 写入 | 0 |
| transfer | 0 |
| direct / non-full erase | 0 / 0 |

`global miss` 与唯一规范化 block 数完全相同，说明每个 block 只在第一次出现时创建。
TLC 没有发生容量淘汰，因此 total hit rate 等于 trace 的历史 prefix 复用率。最终有
50,626 个已创建 block 仍没有存储副本，其余 1,604,765 个 block 已从 DRAM 写入 TLC。

## WA 和 DWPD

本次分析使用外部假设 `SLC WA = 1.2`、`TLC WA = 2.0`：

| 指标 | 结果 |
| --- | ---: |
| 系统入口逻辑写入 | 6,573,117,440 bytes（6.122 GiB） |
| TLC 估算物理写入 | 13,146,234,880 bytes |
| system-equivalent DWPD | 0.043744 |
| TLC logical DWPD | 0.049212 |
| TLC DWPD（WA=2.0） | 0.098425 |
| SLC DWPD | 0 |

SLC 为零是固定写 TLC 控制组的预期结果。这个结果适合与后续 SLC/TLC 分流 Policy
比较，不应解释为原服务某台物理机器的真实 cache hit rate 或真实设备 DWPD。

## 复现

```bash
PYTHONPATH=src python benchmark/swissai_baseline.py \
  /home/solidyang/workspace/G35/datasets/swissai-serving-trace/qwen380b_thinking_bucket-reuse.jsonl \
  --output benchmark/results/qwen380b_thinking_baseline_stats.json \
  --summary benchmark/results/qwen380b_thinking_baseline_summary.json

PYTHONPATH=src python scripts/analyze.py \
  benchmark/results/qwen380b_thinking_baseline_stats.json \
  --slc-wa 1.2 \
  --tlc-wa 2.0 \
  --output benchmark/results/qwen380b_thinking_baseline_analysis.json
```
