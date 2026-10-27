# Qwen Bailian Trace A：共享 DRAM 全量回放

2026-10-27，在 `codex/kimi-k3-lru-k` 工作区运行。全部请求使用同一 Kimi-K3 hybrid profile、
共享 100 GB DRAM、全局 LRU-2；有限 SSD 对照为 RR/adaptive_endurance × 1/4 TB。
本实验替换 workload trace，模型仍为 Kimi-K3。五组全量回放均已完成。
当前回放仅发布输入 prefill 缓存，未发布 decode 输出缓存；以下结果限定于该供给协议，
不能作为完整推理生命周期的命中率结论。

## 输入与身份

使用[阿里官方 Trace A](https://github.com/alibaba-edu/qwen-bailian-usagetraces-anon/blob/5f7439c51ec248a0c585f7d90a41a6f57773b912/qwen_traceA_blksz_16.jsonl)，
revision `5f7439c51ec248a0c585f7d90a41a6f57773b912`，文件 56,354,493 B，SHA-256
`07cedc9ed8aff301994ac68ed4aede8123b7603673575eeba9dd677de663db17`，与该 revision 的
Git LFS oid/size 完全一致。下载与准备的源文件均留在本地 build 目录。

| 输入指标 | 数值 |
|---|---:|
| 请求 | 43,058，全部回放 |
| 原始时间范围 | 0–7,199.464 s |
| root session | 23,101 |
| 输入 token | 100,382,715 |
| 输出 token 记录 | 18,535,289；不据此补造输出 hash |
| 完整 16-token block 引用 | 6,253,601 |
| 不足整块的输入尾部 | 40,375 条请求，共 325,099 token |
| 唯一完整块原始 hash | 2,506,480 |
| 唯一前缀节点 | 2,616,778 |
| 出现在多个父前缀下的原始 hash | 54,810 |
| 唯一 request-end checkpoint 边界 | 41,342 |

保持原始文件顺序，将十进制秒精确转换为纳秒，不缩放或重排时间。`affinity_id` 根据
`parent_chat_id` 追溯 root session，并加 1 避免 0 的回退语义。

原始 `hash_ids` 长度等于 `ceil(input_length/16)`。模拟器只接收完整 block：去除最后不足
整块的 hash，但保留全部 `input_length`。尾部 token 仍计入需求和计算，不丢弃请求。

原始块 hash 不直接作为全局 Radix NodeId。适配器按 `(父前缀身份, 原始块 hash)` 生成
稳定 ID，保留跨 session 的真实相同前缀，并分开不同父上下文。独立稠密数组审计逐项验证了
全部 6,253,601 次引用的父节点与原始内容标签，以及全部请求的时间、长度和 session affinity，
防止块内容相同但前缀不同造成假命中。

## 冻结配置

- DRAM：100,000,000,000 B，`pool_allocation=shared`，两类独立配额均为 0。
- memory：两类 `lru_k`、K=2；实际消费更新历史，发布不计引用；默认淘汰 Dump，SSD 命中回填。
- 模型载荷：FULL 442,368 B/16-token block；LINEAR 449,372,160 B/checkpoint。
- 供给：在每条请求最后完整输入块发布一个 LINEAR checkpoint；不推断 decode hash，
  不假定输出产生额外可恢复 endpoint；恢复上限为 `input_tokens-1`。
- SSD：固定 SLC/TLC=1:2，各 1 stream；总量按 1 MiB 单元向下对齐到名义 1/4 TB。
  `logical_fill_fraction=0.98`，SLC/TLC erase budget=3/7，RR `slc_host_share=3/17`。
- policy 软件分配单元：1 MiB。FULL payload 432 KiB 在 SSD 上分配 1 MiB；LINEAR 按
  429 MiB 分配。软件分配单元与此前设备的 16 KiB NAND page 是两个不同参数。
- 原始时间戳驱动后台维护；参数不按运行中结果调整。每组从空缓存启动，关闭 I/O trace。

## 结果与分母

联合命中要求同一路径的连续 FULL `[0,r)` 和边界 `r` 的 LINEAR 同时可读，来源可为
DRAM 或 SSD。主指标是 `sum(restore_tokens)/sum(input_tokens)`；有恢复请求比例为
`count(restore_tokens>0)/43058`，不表示整条请求完全免计算。

| SSD | storage policy | 联合 token 命中率 | 有恢复请求比例 | 恢复 token | 状态 |
|---|---|---:|---:|---:|---|
| 无限 | infinite_storage | 51.6323% | 55.0560% | 51,829,904 | 完成 |
| 1 TB | RR | 33.5198% | 40.4083% | 33,648,128 | 完成 |
| 4 TB | RR | 47.4425% | 51.1798% | 47,624,048 | 完成 |
| 1 TB | adaptive_endurance | 33.8101% | 39.8532% | 33,939,520 | 完成 |
| 4 TB | adaptive_endurance | 47.3572% | 50.7432% | 47,538,432 | 完成 |

无限 SSD 结果与独立的永久 FULL + request-end endpoint oracle 完全一致。历史完整 FULL
前缀可覆盖 58,189,168 token（57.9673%）；当前供给规则中的 LINEAR endpoint 边界使其中
6,359,264 token 仍需重放。此 oracle 仅是当前供给规则下的上限，不能推广为其他 checkpoint
生成方案的上限。

上述五组总 DRAM 峰值均为 99,999,940,608 B，未超过总预算；FULL 和 LINEAR 共享容量，
未施加 80/20 分区。全部 token 分解、组件 demand、树身份和容量校验通过。

### 无限 SSD 的首轮与后续轮次

逐请求详细结果另行全量回放后，全部最终统计与原无限 SSD 运行完全相同；分组结果也与
独立历史前缀账本一致。按 `parent_chat_id == -1` 划分原始 trace 的 root 请求：

| 输入类别 | 请求数 | 输入 token | 占输入 token | 恢复 token | 联合 token 命中率 |
|---|---:|---:|---:|---:|---:|
| root，无父请求 | 23,101 | 40,129,205 | 39.9762% | 2,732,832 | 6.8101% |
| nonroot，有父请求 | 19,957 | 60,253,510 | 60.0238% | 49,097,072 | 81.4842% |
| 全部 | 43,058 | 100,382,715 | 100% | 51,829,904 | 51.6323% |

约四成输入来自 root，不能将续轮历史前缀的预期命中率套到这些输入。即使 SSD 无限，
当前空缓存回放中此前未发布的数据仍须计算。root 的历史 FULL 前缀有 9,088,128 token，
其中 6,355,296 token 受 LINEAR 端点/恢复上限限制；这占全部 6,359,264 历史匹配但未恢复
token 的 99.9376%。因此 checkpoint 缺口几乎全部来自不同请求共享的前缀，而非续轮
父请求的已生成完整块前缀失配。

全部 19,957 条父子边均保留父请求的完整块前缀，未发现其中的上下文身份转换损坏。
同时，当前供给确实遗漏 decode。实际例子：请求 31 输入 88 token、输出 340 token；
后继请求 88 输入 441 token，当前只恢复父输入中的 80 token。输出长度说明存在需要
核对的历史输出，但不能直接据此声明全部 340 token 均可恢复；还需输出 hash、模板边界
及有效 checkpoint。按长度定位的候选输出窗口中有 8,539,856 token 是当前输入回放首次
看到的完整块，这仅是诊断代理，不能直接加到真实命中分子。

原始分组 [独立账本](../build/joint-boundary-audit/traceA-turns.json)、
[详细结果 native 回放](../build/joint-boundary-audit/traceA-turns-native.json)、
[复核脚本](../build/joint-boundary-audit/replay_by_turn.py)。

RR 1 TB / 4 TB 的 allocated host write 分别为 22.939205 / 22.018765 TB；这是包括软件
padding 的逻辑写入，不是 NAND program bytes 或物理 WAF。本轮未运行全量 MQSim；
缓存命中由上游 DWPDSim 逻辑回放计算，不由 NAND latency 或 WAF 决定。

adaptive 1 TB 的 allocated host write 为 22.891839 TB。该组执行 7 次后台 tick，
idle trim 和迁移均为 0；SLC/TLC 的容量回收段数分别为 20,921 / 15,563。
最终 session gap q95 为 977.190342 s，idle threshold 为 3,447.087561 s。

adaptive 4 TB 的 allocated host write 为 21.928566 TB。该组同样执行 7 次后台 tick，
SLC/TLC idle trim 段数为 14 / 390，容量回收段数为 15,193 / 9,871，迁移为 0。
最终 idle threshold 为 3,435.460734 s。两组 adaptive 的 q95 相同；阈值按策略状态调整。

本实现是共享容量的 LRU-K 基线，不是完整 Gandalf policy。论文数字、轴标签和当前实验的
差异见 [对照核查](gandalf-comparison-audit.md)，不能把 token 命中直接与论文 miss ratio 相减。

## 校验与原始产物

[输入 manifest](../build/qwen-traceA-shared/input/dataset.json)、
[完整引用/session 审计](../build/qwen-traceA-shared/input/audit.json)、
[准备与回放脚本](../benchmark/qwen_bailian_kimi.py)。
结果：[无限 SSD](../build/qwen-traceA-shared/infinite/result.json)、
[RR 1 TB](../build/qwen-traceA-shared/rr-1tb/result.json)、
[RR 4 TB](../build/qwen-traceA-shared/rr-4tb/result.json)、
[adaptive 1 TB](../build/qwen-traceA-shared/adaptive-1tb/result.json)、
[adaptive 4 TB](../build/qwen-traceA-shared/adaptive-4tb/result.json)、
[五组最终审计](../build/qwen-traceA-shared/completed-audit.json)。

native extension SHA-256 为
`f38cf9bd8449d5beda179ead1fc801dcbe9291376e2b6b2112d24bc05cbcdb72`。
模拟器不分配实际 KV tensor；RSS 反映输入缓冲、索引、Radix 元数据和逻辑 SSD 地址。
adaptive 两组使用独立进程并行，内存压力较大时顺序执行；虚拟时间不受影响。
运行耗时可能包含资源调度等待，不作为算法吞吐比较。

## 复现

在 worktree 根目录执行，使用新输出路径保留本轮产物：

```bash
mkdir -p build/qwen-traceA-source
curl --fail --location \
  https://media.githubusercontent.com/media/alibaba-edu/qwen-bailian-usagetraces-anon/5f7439c51ec248a0c585f7d90a41a6f57773b912/qwen_traceA_blksz_16.jsonl \
  --output build/qwen-traceA-source/qwen_traceA_blksz_16.jsonl

.venv/bin/python benchmark/qwen_bailian_kimi.py prepare \
  --source build/qwen-traceA-source/qwen_traceA_blksz_16.jsonl \
  --prepared build/qwen-traceA-reproduction/input

.venv/bin/python benchmark/qwen_bailian_kimi.py audit \
  --prepared build/qwen-traceA-reproduction/input

for tb in 1 4; do
  for policy in wear_share_round_robin adaptive_endurance; do
    .venv/bin/python benchmark/qwen_bailian_kimi.py run \
      --prepared build/qwen-traceA-reproduction/input \
      --output "build/qwen-traceA-reproduction/${policy}-${tb}tb" \
      --storage-policy "$policy" --capacity-tb "$tb" --batch-size 16
  done
done

.venv/bin/python benchmark/qwen_bailian_kimi.py run \
  --prepared build/qwen-traceA-reproduction/input \
  --output build/qwen-traceA-reproduction/infinite \
  --storage-policy infinite_storage --batch-size 16
```

`--emit-io-trace` 可显式输出有限对象 I/O 供后续 MQSim 使用；它不改变缓存策略。
准备与运行目录要求不存在，避免覆盖既有实验。下载地址和版本校验值保存在
[source.json](../build/qwen-traceA-source/source.json)。
