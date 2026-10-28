# Age-conditioned joint memory policy：实现与实验记录

已实现 opt-in 的 `joint_cost`，包含 age 条件复用估计、FULL/LINEAR 联合恢复损失、完整有限 SSD 受害者/迁移计划、tier/stream 选择及边际 NAND 代价代理。所有热路径在 C++，配置错误和选中计划执行错误直接抛出。详细模型和限制见 [DESIGN.md](DESIGN.md)。这是一套实验策略；当前证据不支持将权重1作为推荐配置，也不证明物理 WAF 或真实 TTFT 改善。

## 固定输入与配置

- 运行源码：`4fbc866`；各运行在开始前保存 runtime source SHA256 和 native 副本。全部完成运行的 source/native 未变化，62个源文件的哈希均与该提交一致。
- Native SHA256：`5ed581ad225d43c7c4b02d6443f06ada97ababe903619a169dee461dfe343abe`。
- 原工作区快照另存为 `ad50b96`；开发分支 `codex/age-route-cost` 从 main `f2c2021` 建立。共享 hybrid baseline 单独提交 `994f3a7`。
- 数据：`/home/solidyang/workspace/traceGen/runs/weka-subagent-24h-40tb-v1/dwpdsim`；完整数据70,132请求、14,520,232,256输入token。工作流内时间/共享关系保留，跨工作流相移是实验重建，不代表原始生产全局时序。
- Hash SHA256：`8fdb1f1d274c19fdcff2082c7e1d6b88e703244f2f8c3e54f5490414a6b11e19`；index SHA256：`ab59750b8767fc4af555267a5a713125ccbe3f27962ae3b5b853f5c96a4c8518`。
- Kimi-K3 字节折算：64 token/block，FULL1,769,472 B，LINEAR449,372,160 B。它是 counterfactual 缓存载荷配置，没有真实 Kimi 推理或质量测试。模型配置 revision：`f831ab66814297da540d832a5235f8e904f29d06`。
- 每个输入请求末尾的最后完整block发布一个 checkpoint；prefill-only，不推断 decode hash。所有配置从空缓存开始；每一配对的输入、截止时间、总内存、SSD、存储参数和publication规则相同。
- DRAM100,000,000,000 B共享，无FULL/LINEAR固定配额。baseline为LRU-2且总是Dump；joint_cost使用相同LRU-K冷候选索引并联合选择对象、Drop/Dump和路由。SSD容量按分配单元向下取整，SLC:TLC=1:2。
- 主实验 `[0,3600)`，3,127请求、506,039,360输入token；每池1条stream。三个默认预测头60/300/1800秒均有成熟标签。
- 补充实验 `[0,600)`，504请求，每池2条stream，覆盖1/4TB和RR/adaptive。1800秒头在这组短窗全部删失；它验证路径和配置，不能代表长时域训练后的收益。
- 固定搜索预算：每类1组候选、最多4个路径prototype；seed42、每8请求每类抽32个forecast。generic API默认2组/8路径，单独试跑报告如下，不与主表混用。
- 几何代理匹配参考MQSim：16KiB page、4224 pages/block、OP0.125、Ideal Mapping对应mapping项0。每NAND GiB价格为SLC0.05、TLC0.2等价计算秒，属于研究假设，尚未做设备标定。存储参考配置SLC写入份额3/17、擦写预算3/7；这些是实验参数，不是产品额定PE寿命。

## 数据准备方法

原始WEKA固定revision为 `23f152f6f0f9399a85901b89a6458def0ef16729`，原始JSONL SHA256为 `29b6a19e751ff5230771519aab755f80a0f43a4ba9cf96b72d3a6a437ec99276`。现有准备配置使用seed42、86400秒、workflow发起率0.0031411603444320268/s、Gamma到达CV0.5，并对整个workflow作最多±3600秒的相移以平衡分钟计数；不修改workflow内部间隔或拆散subagent共享。每个抽样实例有独立namespace，同一实例的父/子agent共享该namespace。保留输出长度但不创造decode身份。

已有生成/审计方法位于 `/home/solidyang/workspace/traceGen/docs/weka-sampling.md`；当前配置为 `runs/weka-subagent-24h-40tb-v1/config.json`。生成器provenance记录base `6beb523` 加8个实现文件的SHA256，本次逐个核对全部匹配；不能只用base提交号声明可复现。40.074TB是unique对象按1MiB单元取整的分配规模，不是实际写入量。准备阶段的预历史用于重建到达过程，不给DWPDSim预热KV。

使用新的输出路径重新生成并审计：

```bash
cd /home/solidyang/workspace/traceGen
python3 experiments/plan_weka.py \
  --config runs/weka-subagent-24h-40tb-v1/config.json \
  --output runs/new-version-plan.json
python3 generate.py \
  --config runs/weka-subagent-24h-40tb-v1/config.json \
  --output runs/new-version/trace.jsonl.gz
python3 experiments/audit_weka.py \
  --trace runs/new-version/trace.jsonl.gz \
  --manifest runs/new-version/trace.jsonl.gz.manifest.json \
  --plan runs/new-version-plan.json \
  --output-dir runs/new-version/dwpdsim --report runs/new-version/audit.json
```

审计器逐请求、逐hash引用核对来源并输出 `dataset.json / requests.jsonl / hashes.u64`。DWPDSim本轮直接使用前述已固定digest的连续缓冲；没有再裁剪、改写单个请求的hash路径，仅按时间选择完整请求。

## 一小时配对结果

| Memory / Storage / 容量 | 权重 | 联合 token 命中率 | 分配主机写入 GB | 相对 baseline 写入 | 额外 compute token | 单次回放秒 |
|---|---:|---:|---:|---:|---:|---:|
| baseline / wear_share_round_robin / 1 TB | — | 92.9778% | 2383.607 | 1.0000 | 0 | 69.98 |
| joint_cost / wear_share_round_robin / 1 TB | 0.0 | 91.6125% | 1806.706 | 0.7580 | 6,909,056 | 670.29 |
| joint_cost / wear_share_round_robin / 1 TB | 1.0 | 48.4694% | 480.037 | 0.2014 | 225,230,336 | 484.97 |

联合命中率分母是全部input token，命中要求连续FULL前缀与完整可读LINEAR处于共同恢复点。写入包含padding与迁移，单位为十进制GB。额外compute token是相对同输入baseline的差值，不将独立块命中率当作联合命中率。

权重0保留了大部分恢复收益，同时减少部分主机写入；权重1进一步压缩写入但产生显著命中/重放损失。两者体现取舍，不能仅凭写入下降声称新策略优于baseline。在线cost计数是重叠未来时域下逐次决策估值的累计，不是这次回放真实总服务时间。价格和GC模型未标定，因此不报告加权总成本的实测优胜者。

当前原型的模拟回放开销显著高于baseline。表中时间包含输入适配、构造和finish，不含digest验证；作业并发条件不同，不能作为重复统计的吞吐比，更不是模型TTFT。

## 存储与多stream补充配对

| Memory / Storage / 容量 | 权重 | 联合 token 命中率 | 分配主机写入 GB | 相对 baseline 写入 | 额外 compute token | 单次回放秒 |
|---|---:|---:|---:|---:|---:|---:|
| baseline / adaptive_endurance / 1 TB | — | 86.7674% | 535.832 | 1.0000 | 0 | 12.20 |
| joint_cost / adaptive_endurance / 1 TB | 1.0 | 59.5610% | 119.428 | 0.2229 | 21,438,656 | 79.20 |
| baseline / wear_share_round_robin / 4 TB | — | 86.7674% | 535.832 | 1.0000 | 0 | 18.36 |
| joint_cost / wear_share_round_robin / 4 TB | 1.0 | 65.6440% | 167.819 | 0.3132 | 16,645,312 | 86.45 |
| baseline / adaptive_endurance / 4 TB | — | 86.7674% | 535.832 | 1.0000 | 0 | 9.81 |
| joint_cost / adaptive_endurance / 4 TB | 1.0 | 65.6440% | 167.819 | 0.3132 | 16,645,312 | 69.88 |

相同存储策略在joint_cost中负责容量回收和后台迁移；joint_cost直接比较其可执行tier/stream计划，路由选择也属于新算法的改动。此表不是固定路由下的纯memory-policy消融。600秒窗口不足以覆盖完整设备寿命或默认最长复用时域。

## 估计器与回归检查

权重0/1的逻辑FULL/LINEAR需求及forecast评分完全相同，训练不依赖Drop/Dump或命中结果。预测使用issue时冻结特征，正负标签同样等待完整时域成熟；尾部未成熟标签删失。

| 类型 | 时域秒 | 成熟样本 | 正例率 | Brier均值 | 删失 |
|---|---:|---:|---:|---:|---:|
| full | 60 | 12288 | 0.2162 | 0.115466 | 192 |
| full | 300 | 11552 | 0.3917 | 0.174218 | 928 |
| full | 1800 | 6304 | 0.6788 | 0.219212 | 6176 |
| linear | 60 | 12288 | 0.2680 | 0.095691 | 192 |
| linear | 300 | 11552 | 0.3750 | 0.142300 | 928 |
| linear | 1800 | 6304 | 0.4965 | 0.228068 | 6176 |

Brier是本回放的在线预序列评分，包含冷启动；没有独立跨数据集泛化或置信区间结论。shared模型表约2.13MB，另有每对象history、pending forecast、tree和quote内存，不能解释成仅一字节/对象。

- 全量Python回归533 passed、2 optional skips；Debug CTest7/7；Ruff与diff检查通过，命令记录见PROGRESS.md。
- 对照旧native：两个有限容量合成用例的I/O trace SHA256完全一致；253请求真实数据下权重0和默认搜索权重1，除报价/评估次数外所有native stats完全一致。`quote-equivalence-tiny.json`、`quote-equivalence-weka-w0.json`、`quote-equivalence-weka-w1.json`保存证据。单次时间分别79.82→42.16秒、244.87→89.23秒；并发条件不同，不作为重复吞吐统计结论。
- 公共集成测试覆盖合法恢复、共享容量、失去副本后的逻辑需求、媒体价格改变路由、容量/I/O账本、pending publication、等待未完成Dump、迁移保留storage recency、独立分支概率质量及短于一个block的prompt。
- paired真实MQSim功能回放：48请求/策略，baseline223条命令、joint_cost395条命令，分配字节、完成计数及依赖核对通过。最终native与主实验相同，副本及digest保存在 `build/age-route-cost/device-smoke-final-v6`。小逻辑池置于1TB物理几何，证明转换/执行完成；不证明有限GC下的WAF改善。
- NAND估计使用pool/stream聚合frontier，未精确模拟各plane的条带布局、受害块选择与GC/WL。因此当前主表是逻辑回放结果，不是NAND写入、DWPD、寿命或真实TTFT结论。真实设备测量还需冻结几何、OP、GC/WL、warmup和测量窗，并逐池报告host bytes、NAND/GC programs、erases及完成计数。

## 复现

在开发checkout完成editable native构建后运行；每次使用新的输出目录：

```bash
cd /home/solidyang/.codex/worktrees/dwpdsim-age-route-cost
python3 -m pip --python .venv/bin/python install -e . --force-reinstall --no-deps
.venv/bin/python benchmark/joint_cost_experiment.py \
  --prepared /home/solidyang/workspace/traceGen/runs/weka-subagent-24h-40tb-v1/dwpdsim \
  --output build/age-route-cost/reproduce-rr1-3600s \
  --end-seconds 3600 --capacities 1 \
  --storage-policies wear_share_round_robin --weights 0 1 --streams 1
```

完整配置矩阵可设置 `--capacities 1 4 --storage-policies wear_share_round_robin adaptive_endurance`。预先固定的敏感性扫描可用 `--weights 0 0.25 0.5 1 2 4 8 16 32 64`；本记录没有声称已跑完此完整扫描或24小时新策略回放。

物理功能验证：

```bash
.venv/bin/python benchmark/joint_cost_device_smoke.py \
  --output build/age-route-cost/reproduce-device-smoke \
  --binary /home/solidyang/.codex/worktrees/dwpdsim-kimi-k3/build/mqsim-mapped/MQSim
```

完成运行的原始JSON、配置、日志、源哈希和native副本位于 `build/age-route-cost/final-v6-*`。以前被主动中止的输出含 `aborted.json`，不进入结果表。默认2组/8路径的300秒pilot在 `quote-v6-w1-default-300s`，命中63.4067%；它同样不支持推荐权重1。

固定300秒的indexed/context混合适配对照及同native五组完整比较见 [300秒对照报告](RETENTION_300S.md)。
