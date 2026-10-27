# WEKA 062126 / Kimi-K3 / LRU-2 完整回放

已在 `codex/kimi-k3-lru-k` 分支运行完整下载数据。基于 main `f2c20211c24982560186b989e9c85d9d4beb8e5d`；本次没有修改 simulator/policy 实现，新增可复现的数据准备及回放脚本。

## 数据与配置

- 数据：`semianalysisai/cc-traces-weka-062126`，revision `23f152f6f0f9399a85901b89a6458def0ef16729`，完整文件 1,847,151,435 B，SHA-256 `29b6a19e751ff5230771519aab755f80a0f43a4ba9cf96b72d3a6a437ec99276`。
- **393 条 root trace，98,827 次模型请求**，递归保留主任务与子 agent 的全部 n/s 请求；subagent 容器标记不作为请求。
- **338,052,834 次 hash-block 引用，21,635,381,376 个输入 token 槽**，每块 64 token。`in` 是原数据的块长度代理，不是重新用 Kimi tokenizer 计算的 token 数。全部请求满足 `in == 64 * len(hash_ids)`；没有截断输入或补造输出 hash。
- 所有 root 的 t=0 对齐，然后按 `(timestamp_ns, trace_index, recursive_position)` 稳定合并；nested t 已经相对 root，不再次累加。完整到达时间跨度为 **917,239.434 秒（10.616 天）**。这是重建的同步起跑场景，不能当作原生产全局到达流或一天 trace。
- 不同 root 的 hash namespace 隔离，root 内主/子 agent 共享；将全部原模型调用映射为同一 Kimi-K3 目标模型。原数据的 hash 前缀身份得以保留，这属于目标模型统一的回放实验，不代表原 Claude 多模型部署。
- 空缓存起跑，**FULL 80,000,000,000 B + LINEAR 20,000,000,000 B = 100 GB DRAM**（十进制）。两个 pool 独立 LRU-K，**K=2**，SSD 命中后回填。
- Kimi-K3 whole-model 逻辑 payload：FULL **1,769,472 B/64-token block**；LINEAR **449,372,160 B/checkpoint**。无 TP 复制、真实 tensor 或 allocator padding。来源见 [模型说明](../docs/kimi-k3.md)。
- 每次请求仅在最后完整输入块生成 request-end LINEAR checkpoint；默认最多恢复 `input_tokens - 1`，没有通过给输入长度加 1 改善命中。未推断 decode-only KV、输出 checkpoint、模型耗时或请求完成时刻。
- 驱逐全部 Dump，**infinite_storage**；1 MiB 逻辑地址分配单位；关闭 I/O trace。当前不是有限 1/4 TB SSD 或 MQSim 物理设备实验；config 内 SLC/TLC 占位容量不限制该 backend。

## 结果

| 指标 | 完整数据结果 |
|---|---:|
| 联合恢复 token / 输入 token 槽 | 21,167,276,032 / 21,635,381,376 |
| **FULL ∩ LINEAR 联合 token 命中率** | **97.836390%** |
| 恢复至少一段前缀的请求 | 88,606 / 98,827（89.657685%） |
| 其中恢复源全部来自 DRAM 的请求 | 159（占全部请求 0.160887%） |
| Prefill compute token 槽 | 468,105,344 |
| 因 checkpoint 边界需重放的已匹配前缀 token 槽 | 102,205,504 |
| 全新后缀 token 槽 | 365,899,840 |
| 生成 checkpoint 次数 / 唯一观察末端 | 98,827 / 98,208 |
| 唯一 FULL 对象 | 5,717,185 |

真正恢复边界始终同时要求：从起点连续可读的 FULL 前缀，以及同路径、同边界可读的 LINEAR checkpoint。上表的请求比例表示至少恢复一段前缀，并不表示整个输入都命中。DRAM/SSD 可以混合构成合法恢复。

| Component | FULL | LINEAR |
|---|---:|---:|
| 最终 DRAM 对象数 | 45,211 | 44 |
| 峰值 DRAM payload（GB） | 79.999599 | 19.772375 |
| 实际恢复 demand 的 DRAM 命中率 | 62.402288% | 10.806266% |
| SSD payload read（TB） | 220.034162 | 35.514331 |
| SSD payload write（TB） | 10.116397 | 44.131492 |
| SSD 分配后 read（TB） | 260.781229 | 35.551234 |
| SSD 分配后 write（TB） | 11.989804 | 44.177349 |

GB/TB 都采用十进制。Component 命中率分母仅为实际恢复时的消费 demand，不能相加，也不能替代联合 token 命中率。

SSD 合计 payload read **255.548493 TB**，payload write **54.247889 TB**；1 MiB 分配后分别为 **296.332463 TB**、**56.167153 TB**。这些是逻辑 I/O，不是 NAND program、WAF 或真实设备耗时。此轮保留全部 SSD 副本，最终 payload 占用也为 54.247889 TB，明显超过 1/4 TB；有限 SSD 下不能沿用本轮命中率。

本轮 LINEAR 占 payload write 的 **81.3515%**。这是后续 checkpoint admission/retention 与 Dump/Drop 代价建模的重要观测；仅凭这一轮不能证明任何新策略优于 LRU-2。无限 SSD 且全部 Dump 时，总联合恢复由历史 checkpoint 覆盖决定，LRU-K 主要改变 DRAM 来源和读写开销。

## 运行开销与校验

数据准备 **32.157 秒**。回放 **154.520 秒**，**639.57 请求/秒**，其中 aggregate pybind/native 调用合计 **138.805 秒**。回放计时包含 simulator 构造、mmap 读取、Python 适配、binding 构造及 native 回放；不含预处理、index 加载、close 和最终序列化。原始结果的计时范围文字在审计中修正，数值及 counters 未改变，原始 JSON 留存为 `result.raw.json`。

进程峰值 RSS **4,197,720,064 B（3.909 GiB）**，包含 metadata、request index、mmap 页面和 batch buffer；没有分配 100 GB KV tensor。这是单次实际回放开销，未做受控性能 A/B，不代表模型服务吞吐。

- 独立 endpoint-set oracle 与 native 的 request、input、restored token/request、compute token、checkpoint 数逐项精确一致。
- token 分解、FULL/LINEAR demand、驱逐分类、pool 容量及 allocated/payload I/O 关系全部通过；无 I/O trace 文件。
- prepared index SHA、排序、offset 完整覆盖、文件长度及 namespace 抽样通过。
- Native binary SHA-256：`28d7e3d5c8549d6d5dad1bd9ee7ad5a83f93985423ca9207513b1961f46353fc`。

## 复现和产物

在此 worktree 根目录执行：

```bash
.venv/bin/python benchmark/weka_kimi.py prepare \
  --source /home/solidyang/workspace/G35/datasets/cc-traces-weka-062126/traces.jsonl \
  --prepared build/weka-kimi-062126/input

.venv/bin/python benchmark/weka_kimi.py run \
  --prepared build/weka-kimi-062126/input \
  --output build/weka-kimi-062126/lru-2 \
  --pool-allocation fixed --memory-bytes 100000000000 \
  --full-bytes 80000000000 --linear-bytes 20000000000 --k 2 --batch-size 64

.venv/bin/python build/weka-kimi-062126/joint_oracle.py
```

- [准备与回放脚本](../benchmark/weka_kimi.py)
- [完整数据 manifest](../build/weka-kimi-062126/input/dataset.json)
- [实际配置](../build/weka-kimi-062126/lru-2/config.json)
- [完整结果及审计](../build/weka-kimi-062126/lru-2/result.json)
- [原始结果](../build/weka-kimi-062126/lru-2/result.raw.json)
- [独立联合命中 oracle](../build/weka-kimi-062126/joint-oracle.json)
- [校验结果与派生指标](../build/weka-kimi-062126/audit.json)
