# Gandalf 命中指标与当前实验对照

2026-10-27 核对 [Gandalf 原文](https://arxiv.org/pdf/2610.30791)，并查看 Figure 7 原图。
该图横轴是 cache **miss** ratio。§5.9 的 Trace A、α=1 数值为 0.823，按 miss 取补数是
17.7% 命中；Figure 7 的 Trace A 最低 miss 点约为 0.67–0.68，而非接近 95% 命中。
约 95% 对应 Claude Code；例如 §5.2 的 0.052 miss，取补数为 94.8%。
名义主配置确实同为 Kimi-K3、100 GB DRAM、1 TB SSD。§5.1 的指标文字把 miss 描述为
从缓存服务的请求比例，与指标名称矛盾；在取得原实现前，不能断言其分母与本实验一致。

## 论文明确展示的实验矩阵

| 图/表 | DRAM | SSD | 数据 | 模型/比较 |
|---|---|---|---|---|
| Figure 7、§5.4 | 100 GB | 1 TB | Trace A、ClaudeCode、AIPerf | Kimi-K3；Gandalf 与六个基线，扫描超参 |
| Figure 9、§5.5 | 100 GB | 4 TB | AIPerf | 换混合模型；Gandalf、LRU-K K=1、HotPrefix F=1 |
| Figure 8、§5.3 | 200/300/400/500/1000 GB | 3800/3700/3600/3500/3000 GB | Trace A | Kimi-K3；保持 DRAM+SSD=4 TB，并非 SSD=4 TB |
| Figure 10/11、§5.6 | 100 GB | 1 TB | ClaudeCode | Qwen3.5-27B；写入有效性与命中来源 |
| Figure 12、§5.7 | 100 GB | 1 TB | Trace A | Kimi-K3；各策略写入分解 |
| Figure 13/14、§5.8 | 100 GB | 1 TB | ClaudeCode | Kimi-K3；载入延迟和费用 |
| Table 3 | — | 1 TB / 4 TB | 三份 trace | Qwen3.5-27B / Kimi-K3 的 required DWPD；不是逐 policy 命中率对比 |

论文 §5.1 宣称容量取 1/4 TB，但正文未提供与 Figure 7 完全对称的 4 TB 三数据集主对照图。
三份 trace 的名字为 `qwen_traceA_blksz_16`、`cc-traces-weka-062126`、
`aiperf-100r-500o-noshare`。WEKA 引用的公开来源是
[SemiAnalysis HF](https://huggingface.co/datasets/semianalysisai/cc-traces-weka-062126)；
Trace A 来自[阿里数据仓库](https://github.com/alibaba-edu/qwen-bailian-usagetraces-anon)；
AIPerf 是[工具](https://github.com/ai-dynamo/aiperf)生成的 trace，论文没有给完整生成命令。

§4 的主评估设置为 1 DWPD × 5 年、WAF=1，累计 host write 后按 trace 时长折算日写入量
并估计寿命；前期 FEMU 验证使用的 1 TB / 0.3 DWPD / 5 年是另一组设置。主实验不是
我们固定 SLC/TLC 双池的 NAND 模拟。§5.8 的延迟模型参数是 SSD→DRAM 10 GB/s、
DRAM→GPU 50 GB/s、重算 60k token/s；迁移与重算重叠，取二者耗时最大值。

精确复现仍缺：checkpoint/输出缓存发布协议、miss 计数公式、WEKA 全局时间线构造、
AIPerf 完整生成参数。源包仅有论文材料；本次搜索未找到可核验的作者模拟器仓库，
不能据此断言作者没有公开仓库。

如果观察的是 Figure 10，纵轴是 writes hit ratio：写入 SSD 的对象中，之后至少被命中一次
的比例。它衡量写入的有效性，不是全部请求或输入 token 的命中率。

## Figure 7 尚未完成复现

Figure 7 的 Trace A、Kimi-K3、100 GB DRAM / 1 TB SSD 名义配置与当前有限 SSD 实验
相符，但当前实现不能称为该图的论文基线或 Gandalf policy 复现。

| 对照项 | 论文 | 当前实现 | 状态 |
|---|---|---|---|
| LRU-K | §5.7 明确：对象被引用 K 次后才允许写 SSD，否则 Drop | LRU-K 仅用于 DRAM victim 排序；没有引用次数准入门槛，未备份对象默认 Dump | 已证实不同 |
| Gandalf | 代价驱动 Drop/Move 与 SSD admission；α 控制成本取舍 | 尚未实现完整代价策略；memory LRU-2 配合 RR/adaptive_endurance | 已证实不同 |
| SSD | 统一 SSD 策略 | SLC/TLC 固定 1:2 两池，1 MiB 分配单位 | 组织不同，论文精确分配单位未确认 |
| 供给 | 完整 checkpoint 发布协议未给出可执行定义 | 仅输入完整块末端 checkpoint，无 decode 发布 | 未对齐 |
| miss 分母 | §5.1 使用 requests 且误将 miss 描述为 cache-served fraction；§5.7 又使用 chunks | `sum(input_tokens-restore_tokens)/sum(input_tokens)` | 未证实一致 |

本地未备份对象的 Dump 分支见 `cpp/src/hybrid_simulator.cpp:218-233`；LRU-K 选择
victim 的分支见 `cpp/src/policies/memory/hybrid_memory_policy.cpp:111-115`。
更换 shared DRAM 只纠正了容量约束，并未把该组合变成论文的 LRU-K 准入策略。

当前无限 SSD 的 token miss 为 48.3677%；1 TB RR 为 66.4802%，adaptive 为 66.1899%。
Figure 7 左图 Trace A 最低 miss 图估读约 0.67，默认 α=1 的正文 §5.9 数字为 0.823。
无限 SSD 不能直接对照有限 1 TB 图；1 TB 数字接近某个论文点也不构成协议复现。

正文 §5.7 同名义配置的写入分析进一步说明目标不同：Gandalf α=1 只写入 25 个 LINEAR
状态，LRU 写入约 90k 个，并报告 Gandalf miss 0.818、LRU 0.697。当前 RR 1 TB 写入
42,281 次 LINEAR 对象；这是当前事件流的统计，不能先假定与论文“状态数”完全同义。
论文主动放弃部分 LINEAR 可用性以减少写入，因此不能期待默认 Gandalf 与 Dump-all
具有相同命中率。§5.7 的 0.818 与 §5.9 的 0.823 也存在未解释的差异，不应混成同一个
精确复现目标。

分母的一个旁证：用原 trace 6,253,601 个完整块引用作分母，论文 §5.7 四舍五入后的
1.11M / 1.91M FULL hits 对应 miss 0.8225 / 0.6946，接近其 reported miss。这支持
按 chunk/token 而非仅整请求计数核对，但四舍五入数据和版本差异不足以确认公式。
需要原始结果/代码或作者给出的精确计数定义，不能通过选择某个分母强行对齐。

## Figure 9 的低 miss 对应哪个实验

[§5.5 与 Figure 9](https://arxiv.org/pdf/2610.30791#page=9) 明确说明该图使用 **AIPerf**，
配置为 100 GB DRAM + 4 TB SSD，而非 Bailian Trace A。[§5.1](https://arxiv.org/pdf/2610.30791#page=8)
说明 AIPerf 有 400k 请求，每个会话 100 轮；表 3 的 trace 名为 `aiperf-100r-500o-noshare`。
图中各子图改变模型的 LINEAR state / FULL chunk 字节比，不能据此假定不同 workload
具有相同的可复用 token 比例。

长会话可使新 token 在全部输入 token 中的占比很低。用本实验 token 分母举一个理想例子：
会话有 100 轮，每轮输入保留全部历史并追加 Δ token，每轮开始时上轮输入的 FULL 和
末端 LINEAR 都已保存。累计输入为 `Δ × (1+...+100)=5050Δ`，首次计算为 `100Δ`，
因此无限缓存的 token miss 为 `100/5050=1.9802%`。这是解释复用结构的数学例子，
不是 Figure 9 的实际生成参数或对论文指标分母的确认。

当前 Trace A 的 23,101 / 43,058 条请求没有父请求，root 输入占全部输入 token 的
39.9762%；原协议下 root 和 nonroot 分别命中 6.8101% 与 81.4842%。因此 51.6323%
不能用来直接对照 AIPerf 的低 miss。公平复现 Figure 9 需要使用同一 AIPerf trace、
100 GB / 4 TB 容量和相同供给/分母；当前 decode 供给缺口仍需单独处理。

## 当前结果的精确定义

本地 Qwen Bailian Trace A 使用全部 43,058 条请求、原始 7,199.464 s 时间线，token 指标是：

```text
joint_token_hit_rate = sum(restore_tokens) / sum(input_tokens)
joint_restored_request_rate = count(restore_tokens > 0) / requests
```

恢复位置必须同时满足连续 FULL 前缀与同边界 LINEAR checkpoint。第二个指标仅表示请求
恢复了至少一个有效前缀，不表示整条输入完全命中或无需计算。

已完成的全量运行：

| 后备存储 | 联合 token 命中率 | 至少恢复一段的请求比例 |
|---|---:|---:|
| 无限 SSD | 51.6323% | 55.0560% |
| RR，1 TB | 33.5198% | 40.4083% |
| RR，4 TB | 47.4425% | 51.1798% |
| adaptive，1 TB | 33.8101% | 39.8532% |
| adaptive，4 TB | 47.3572% | 50.7432% |

无限 SSD 的恢复 token 为 51,829,904 / 100,382,715，与独立历史 endpoint oracle 完全一致。
此上限仅适用于当前输入供给模型：只在最后完整输入块保存 checkpoint，没有推断输出
token 的 hash 或 decode checkpoint。历史完整 FULL 前缀覆盖率为 57.9673%；其中缺少可用
endpoint 边界导致 6,359,264 个历史匹配 token 仍需计算。

原始数据及全部引用审计见
[dataset.json](../build/qwen-traceA-shared/input/dataset.json)、
[audit.json](../build/qwen-traceA-shared/input/audit.json)。RR 原始结果：
[1 TB](../build/qwen-traceA-shared/rr-1tb/result.json)、
[4 TB](../build/qwen-traceA-shared/rr-4tb/result.json)。

## 相同名义容量尚不等于同一个实验

| 项目 | 当前实现与证据 | 对照状态 |
|---|---|---|
| 模型 | Kimi-K3；FULL 442,368 B/16 token，LINEAR 449,372,160 B/state | 模型名称一致；原文没有足够 dtype/layout 细节确认全部字节参数 |
| DRAM | 共享总量 100,000,000,000 B，无类型配额和软目标 | 名义总量一致 |
| SSD | 固定 SLC/TLC 1:2，两池分别回收，logical fill 0.98 | 当前是双池实验，不能等同于统一 SSD |
| 分配 | 每对象按 1 MiB 取整；FULL 442,368 B 分配为 1,048,576 B | 尚无论文相同取整规则的证据 |
| memory policy | 全局 LRU-2 淘汰排序，默认驱逐 Dump | 完整 Gandalf 代价估计/准入未实现；不能将本基线称为 Gandalf |
| checkpoint 供给 | 仅输入完整块末端；输出长度不生成额外对象 | 需与论文原实现核对 |
| 指标 | 上述两个明确定义的联合指标 | 需核对论文具体计数与分母 |

源代码：[输入适配与审计](../benchmark/qwen_bailian_kimi.py)、
[模型载荷](../src/dwpdsim/model_profiles.py)、
[恢复与供给](../cpp/src/hybrid_simulator.cpp)。

此前有限 WEKA 结果只覆盖前 1,000 条、11.758 s，不能与论文完整 Claude Code 结果比较。
另一次历史完整 WEKA、无限 SSD 的联合 token 命中率为 97.8364%，见
[完整回放报告](weka-kimi-k3-lru2.md)。那次 SSD payload 占用约 54.25 TB，不能把其命中率
套到有限 1/4 TB 配置。短窗口、容量和工作负载不同必须分开讨论。

MQSim 位于本次逻辑命中计算的下游，因此 NAND 时序和设备 WAF 不是这些逻辑命中率数值
的直接原因。后续公平对照首先需要确认同一 trace 范围、模型字节数、供给规则、分配单位、
有效容量、候选/准入算法和指标分母，不能仅对齐 100 GB / 1 TB 两个数值。

## 无限容量与恢复边界核验

联合命中采用连续 FULL 前缀内最深的可读 LINEAR 端点。输入 A B C D、缓存 FULL A/B/C
与 LINEAR B 时，恢复 A B，计 2 个单位；无需 LINEAR A。用当前实验同一 native 扩展
通过公开 API 分别构造全 DRAM、混合层、全 SSD 三种状态，均得到
`matched_prefix_tokens=3, restore_tokens=2, prefill_compute_tokens=2`。
原始结果见 [边界核验](../build/joint-boundary-audit/result.json)。未修改模拟器。

独立无限存储账本对完整 Trace A 的分解为：

| 输入 token 类别 | token 数 | 占全部输入 |
|---|---:|---:|
| 联合恢复前缀 | 51,829,904 | 51.632299% |
| 历史 FULL 前缀中未恢复的部分 | 6,359,264 | 6.335019% |
| 此前输入回放未发布的后缀及不完整块尾部 | 42,193,547 | 42.032682% |

第二行包含 6,315,968 个缺少可用端点的 token，以及 43,296 个因 `r < input_tokens`
恢复上限而损失的 token。仅放宽该上限至 `r <= input_tokens` 的独立诊断为
51,873,200 / 100,382,715 = 51.675430%；不是重新运行修改后的模拟器。
账本及受影响请求示例见 [Trace A 账本](../build/joint-boundary-audit/traceA-ledger.json)。

第三行的“此前未发布”限定于当前输入回放，不能解释成真实推理中从未产生的 token：
适配器仅将 `output_length` 汇总到清单，未向模拟器发布 decode FULL 块及 decode 结束的
LINEAR 状态。因此 51.632299% 仅是当前 prefill-only 供给协议下的结果，尚不能称为真实
完整推理生命周期的无限 SSD 命中率上限。原始输出哈希和完成时间未提供，不能把下一轮
输入无条件反填为上一轮输出。官方 FAQ 还说明跨轮模板会移除 think 特殊 token，完整
decode 重建需要验证实际 token 边界及可用时间。

随后逐请求全量复核，将 root 与有父请求的续轮分开：前者占全部输入 token 的
39.9762%、命中 6.8101%；后者占 60.0238%、命中 81.4842%。按 token 加权即
51.6323%。全部 19,957 条父子边的父输入完整块前缀一致。详细 native 回放与原实验的
全部最终统计相同，分组也与独立账本相同，见
[分组与输入/输出示例](qwen-bailian-traceA-shared.md#无限-ssd-的首轮与后续轮次)。
这些事实解释当前协议下的 51.6323%，并未补全 decode 供给缺口。
