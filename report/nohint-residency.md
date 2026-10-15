# No-hint 概率软驻留实验

本轮完成24组完整回放，以命中率、逻辑写入量和重算代价作为核心指标。实验策略未替换默认策略；未提交或推送。

## 结论

1. 没有观察到同一预测策略在两份数据、两个容量上稳定同时改善三个核心指标。
2. 仅按返回概率排序没有稳定收益。unscaled 256 GiB 下，它的命中率更低、写入更多、重算代价更高。
3. 加入恢复代价权重后，256 GiB 下命中率和重算代价改善，但写入量增加，属于明确的代价交换。不能只凭命中率或重算代价宣布胜出。
4. 前缀保留项的区分能力有限：unscaled 训练集保留率全部为1；TraceLab 合成数据的整体保留率约99.1%。unscaled 的 return-60 和 prefix-60 回放指标完全一致。
5. 固定300s Dump/Drop规则存在容量耦合：内存增大会推迟回收，更多受害段超过300s后被Drop，所以256 GiB对照的总命中率反而低于128 GiB。dump-all参考在两个容量下的命中率和计算代价一致，表明这不是容量增加本身造成的损失；预测排序只能部分缓解该规则的影响。

## 核心结果：预设60秒窗口的完整评分与LRU比较

命中率采用现有连续前缀总命中口径，包含 Memory 和 Storage；不是单独的 Memory 命中率。写入为逻辑 WRITE 字节，所有 block 均为8 MiB，因此块数与字节数的变化比例相同。重算代价为原有 `compute_cost`，即连续前缀 miss 数乘请求上下文 block 数的累计 block² 代理，不是真实推理时延。该计数包含首次冷miss的计算部分，各策略以同一输入对照；减去dump-all参考可得到本实验中额外的计算开销。

所有变化均相对相同数据、相同容量、开启全部回填的 LRU/300s 对照。写入和重算变化为负表示减少。

| 数据 | GiB | LRU命中率 % | value-60命中率 % | 命中变化 pp | 写入变化 % | 重算变化 % |
|---|---:|---:|---:|---:|---:|---:|
| unscaled | 128 | 95.1279 | 95.1319 | +0.0041 | -0.01 | -0.09 |
| unscaled | 256 | 93.9029 | 94.5674 | +0.6645 | +54.77 | -19.16 |
| tracelab | 128 | 94.6402 | 94.6402 | +0.0000 | +0.00 | +0.00 |
| tracelab | 256 | 94.2724 | 94.5390 | +0.2665 | +14.45 | -7.71 |

![三个核心指标](../build/residency-nohint/core_metrics.png)

## 全部24组

write 为累计逻辑写入 TiB；cost 为10⁹ block²。评估结束不强制刷出仍驻留的缓存，因此很小的写入差异也可能受窗口边界的未刷出库存影响。各组同时保留相同的窗口边界规则。

| 数据 | GiB | 策略 | 命中率 % | 写入 TiB | 写入变化 % | cost | 重算变化 % |
|---|---:|---|---:|---:|---:|---:|---:|
| unscaled | 128 | dump-all | 95.1319 | 13.610 | +0.03 | 0.7271 | -0.09 |
| unscaled | 128 | lru-300 | 95.1279 | 13.606 | +0.00 | 0.7278 | +0.00 |
| unscaled | 128 | return-60 | 95.1267 | 13.601 | -0.04 | 0.7280 | +0.03 |
| unscaled | 128 | prefix-60 | 95.1267 | 13.601 | -0.04 | 0.7280 | +0.03 |
| unscaled | 128 | value-60 | 95.1319 | 13.605 | -0.01 | 0.7271 | -0.09 |
| unscaled | 128 | value-300 | 95.1319 | 13.604 | -0.01 | 0.7271 | -0.09 |
| unscaled | 256 | dump-all | 95.1319 | 13.513 | +137.29 | 0.7271 | -30.94 |
| unscaled | 256 | lru-300 | 93.9029 | 5.695 | +0.00 | 1.0528 | +0.00 |
| unscaled | 256 | return-60 | 93.8827 | 6.213 | +9.10 | 1.0812 | +2.69 |
| unscaled | 256 | prefix-60 | 93.8827 | 6.213 | +9.10 | 1.0812 | +2.69 |
| unscaled | 256 | value-60 | 94.5674 | 8.814 | +54.77 | 0.8511 | -19.16 |
| unscaled | 256 | value-300 | 94.5472 | 8.807 | +54.65 | 0.8568 | -18.62 |
| tracelab | 128 | dump-all | 94.6402 | 21.046 | +0.00 | 1.5678 | +0.00 |
| tracelab | 128 | lru-300 | 94.6402 | 21.046 | +0.00 | 1.5678 | +0.00 |
| tracelab | 128 | return-60 | 94.6402 | 21.046 | +0.00 | 1.5678 | +0.00 |
| tracelab | 128 | prefix-60 | 94.6402 | 21.046 | +0.00 | 1.5678 | +0.00 |
| tracelab | 128 | value-60 | 94.6402 | 21.046 | +0.00 | 1.5678 | +0.00 |
| tracelab | 128 | value-300 | 94.6402 | 21.045 | -0.00 | 1.5678 | +0.00 |
| tracelab | 256 | dump-all | 94.6402 | 20.927 | +24.68 | 1.5678 | -9.82 |
| tracelab | 256 | lru-300 | 94.2724 | 16.785 | +0.00 | 1.7385 | +0.00 |
| tracelab | 256 | return-60 | 94.2614 | 17.295 | +3.04 | 1.7518 | +0.76 |
| tracelab | 256 | prefix-60 | 94.2544 | 17.281 | +2.96 | 1.7567 | +1.04 |
| tracelab | 256 | value-60 | 94.5390 | 19.209 | +14.45 | 1.6046 | -7.71 |
| tracelab | 256 | value-300 | 94.5354 | 19.221 | +14.51 | 1.6067 | -7.58 |

## 方法与实验边界

- 输入仍是 daily-mixed-2m-unscaled 和 daily-mixed-tracelab-24h；原始 JSONL SHA256、评估 Parquet SHA256 和模型保存在 provenance.json。
- 各用自身[0,6h)训练，冻结模型；[6h,24h)从空缓存和空session历史评估，分别144,521和144,917个请求。
- 严格 no hint：策略输入只有 timestamp_ns、request_id、affinity_id 和 hash_ids。task、current_user_message_count、is_agent_turn_end 和未来请求只可用于离线归因，不进入策略或拟合分组。训练只使用训练窗口内已经发生的相邻请求。
- 128/256 GiB、8 MiB/block、infinite_storage，所有配置开启 Storage hit 回填。尾部64-block回收上限、禁止向父段回收、冷候选预算alpha=0.1。
- dump-all 是无限Storage的全写盘参考；lru-300 是本轮主要对照。其他四组与lru-300使用相同的300s Dump/Drop规则，仅改变容量回收时的候选价值。
- return-60 使用全1前缀表；prefix-60使用历史前缀保留率；value-60再乘恢复代价；value-300仅将预测窗口改为300秒。所有参数预先固定，未根据评估结果调参。

## 概率如何决定驻留

对每个候选回收block，基于上次访问后的已等待时间a，计算未来h秒的返回概率；按返回间隔桶乘对应的前缀保留概率。分组由上次session间隔（首次/≤60s/>60s）和上下文大小三档组成，共九组。前缀位置分成上下文的三个相对深度区间。

候选价值为实际回收尾部的 `sum(P(block复用) × 恢复代价) / (block数 × h)`。容量不足时重新计算，低价值段先回收，其余继续驻留；空闲时不主动清理。这是压力触发的软驻留，没有新增硬过期定时器，`retention_ns`依然只控制淘汰后的Dump/Drop。

恢复代价采用显式代理：有Storage副本，或按当前规则会Dump时，单block读取代价为1；无副本且会Drop时，取最近请求上下文block数，recompute_weight=1。这个交换率不是测量得到的硬件时延，且评分没有显式加入逻辑写盘惩罚。因此，当前机制容易用更多写入换取更少重算；在以三个核心指标联合优化时，不宜直接作为默认策略。

下一步应把写入代价纳入决策，并在相同写入预算或相同重算预算下比较，而不是只提高保留概率或延长预测窗口。

## 模型限制与验证

- session间隔使用Kaplan–Meier，训练边界右删失；前缀保留只统计训练窗口内观察到的相邻请求，不把窗口末尾未返回当作前缀失效。
- 模型是九组粗粒度统计，已等待120秒后的未来60秒返回概率存在低估。详细离线校准见forecast_check.json；未用这部分评估结果反调本轮模型。
- 共享block只取最近访问者；没有多个owner联合预测，也没有在观测到session改分支时直接让旧分支失效。连续前缀的非线性收益也仅由线性代价近似。
- 两份数据均为合成hash trace，前缀统计不能直接代表生产KV缓存；结果只支持这两个工作负载上的判断。
- 24组配置、请求计数、冷block、物理block计数与前缀统计已核对；四组LRU完整统计与上一轮admit-all一致；unscaled两个容量下的全1前缀消融结果一致。
- 267项Python测试、Debug CTest 4/4、Ruff和git diff检查通过。原生扩展SHA256在回放前后保持一致。
- 并发实验的elapsed_s不用于性能排名；固定2048请求的小型串行热路径核对单独保存在hotpath_check.json。

## 产物与复现

- [策略及配置说明](../docs/reuse-policy.md)
- [核心指标图](../build/residency-nohint/core_metrics.png)
- [逐组相对LRU对照JSON](../build/residency-nohint/comparisons.json)
- [核心指标CSV](../build/residency-nohint/core_metrics.csv) / [配置和计数核对](../build/residency-nohint/final_verification.json)
- 辅助诊断：[实际驻留时长CDF](../build/residency-nohint/residency_cdf.png)、[预测校准](../build/residency-nohint/forecast_calibration.png)。驻留CDF按已结束的block驻留周期加权，不包含结束时仍驻留的周期；不以这些辅助指标选优。

```sh
build/test-venv/bin/python benchmark/residency_experiment.py prepare
build/test-venv/bin/python benchmark/residency_experiment.py run --workers 4
build/test-venv/bin/python benchmark/check_residency_forecasts.py
python benchmark/report_residency.py --output build/residency-nohint
```

绘图Python环境需要matplotlib和numpy。所有实验数据保存在忽略目录build/residency-nohint，未提交生成产物。
