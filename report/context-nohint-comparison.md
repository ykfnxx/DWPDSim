# Context LRU 与概率驻留策略：同口径对照

全部使用同一[6h,24h)评估窗口，空缓存、空session历史起步。128/256 GiB、8 MiB/block、infinite_storage、开启Storage命中回填、alpha=0.1、每次最多回收尾部64 blocks、不向父段回收；finish不刷剩余缓存。策略没有task、用户轮次或agent结束hint。

旧context-lru-retention报告使用全天数据、关闭回填、alpha=0.01，不能与这里的概率模型结果直接拼表。因此补跑8组context LRU，复用已验证的预测策略与LRU结果。

- context-b0：按浅上下文优先选择冷候选，基础retention=300s，beta=0。
- context-b20：相同候选选择，retention=300s+20秒×上次已完成请求的非负context增量（block）；动态回收粒度关闭。这是含增长自适应的对照。
- value-60/value-300：返回概率×前缀保留概率×恢复代价，数字是预测窗口，Dump/Drop阈值仍为300s。
- dump-all：LRU选择、不Drop，作为下表写入和重算变化的共同基线；lru-300：LRU选择、固定300s。

命中率为Memory+Storage连续前缀block命中率。写入是Storage逻辑写入，不含Memory回填或SSD内部写放大。compute_cost为包含冷miss计算的block²代理；相对同容量dump-all报告变化。

| 数据 | GiB | 策略 | 命中率 % | Storage写入 TiB | Storage写入变化 % | 重算代价变化 % |
|---|---:|---|---:|---:|---:|---:|
| unscaled | 128 | dump-all | 95.1319 | 13.610 | +0.00 | +0.00 |
| unscaled | 128 | lru-300 | 95.1279 | 13.606 | -0.03 | +0.09 |
| unscaled | 128 | value-60 | 95.1319 | 13.605 | -0.04 | +0.00 |
| unscaled | 128 | value-300 | 95.1319 | 13.604 | -0.04 | +0.00 |
| unscaled | 128 | context-b0 | 95.1319 | 13.599 | -0.08 | +0.00 |
| unscaled | 128 | context-b20 | 95.1319 | 13.603 | -0.05 | +0.00 |
| unscaled | 256 | dump-all | 95.1319 | 13.513 | +0.00 | +0.00 |
| unscaled | 256 | lru-300 | 93.9029 | 5.695 | -57.86 | +44.80 |
| unscaled | 256 | value-60 | 94.5674 | 8.814 | -34.78 | +17.06 |
| unscaled | 256 | value-300 | 94.5472 | 8.807 | -34.83 | +17.83 |
| unscaled | 256 | context-b0 | 94.4153 | 8.815 | -34.77 | +25.59 |
| unscaled | 256 | context-b20 | 94.9583 | 12.795 | -5.31 | +5.56 |
| tracelab | 128 | dump-all | 94.6402 | 21.046 | +0.00 | +0.00 |
| tracelab | 128 | lru-300 | 94.6402 | 21.046 | +0.00 | +0.00 |
| tracelab | 128 | value-60 | 94.6402 | 21.046 | +0.00 | +0.00 |
| tracelab | 128 | value-300 | 94.6402 | 21.045 | -0.00 | +0.00 |
| tracelab | 128 | context-b0 | 94.6402 | 21.036 | -0.04 | +0.00 |
| tracelab | 128 | context-b20 | 94.6402 | 21.036 | -0.04 | +0.00 |
| tracelab | 256 | dump-all | 94.6402 | 20.927 | +0.00 | +0.00 |
| tracelab | 256 | lru-300 | 94.2724 | 16.785 | -19.79 | +10.89 |
| tracelab | 256 | value-60 | 94.5390 | 19.209 | -8.21 | +2.35 |
| tracelab | 256 | value-300 | 94.5354 | 19.221 | -8.15 | +2.49 |
| tracelab | 256 | context-b0 | 94.4860 | 19.244 | -8.04 | +4.75 |
| tracelab | 256 | context-b20 | 94.6123 | 20.626 | -1.43 | +0.87 |

## 直接相对value-60的变化

命中率单位为百分点；Storage写入和重算变化为负表示减少。

| 数据 | GiB | 策略 | 命中变化 pp | Storage写入变化 % | 重算变化 % |
|---|---:|---|---:|---:|---:|
| unscaled | 128 | context-b0 | +0.0000 | -0.04 | +0.00 |
| unscaled | 128 | context-b20 | +0.0000 | -0.01 | +0.00 |
| unscaled | 256 | context-b0 | -0.1521 | +0.01 | +7.29 |
| unscaled | 256 | context-b20 | +0.3909 | +45.17 | -9.82 |
| tracelab | 128 | context-b0 | +0.0000 | -0.05 | +0.00 |
| tracelab | 128 | context-b20 | +0.0000 | -0.05 | +0.00 |
| tracelab | 256 | context-b0 | -0.0529 | +0.18 | +2.35 |
| tracelab | 256 | context-b20 | +0.0733 | +7.38 | -1.44 |

![核心指标](../build/context-nohint/core_comparison.png)

[CSV](../build/context-nohint/comparison.csv) · [JSON](../build/context-nohint/comparison.json) · [配置与输入校验](../build/context-nohint/provenance.json)

本次只比较预先指定的beta=0/20，未扫描其他beta或alpha；不宣称得到全局最优参数。完整配置和逐请求统计在build/context-nohint内。生成产物不提交。

```sh
build/test-venv/bin/python benchmark/context_nohint_comparison.py run --workers 8
python benchmark/report_context_nohint.py
```
