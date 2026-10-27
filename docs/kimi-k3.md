# Kimi-K3 模型载荷与混合恢复

`kimi_k3_profile(tokens_per_block=...)` 固定一个 Kimi-K3 缓存格式：压缩 MLA 使用 BF16，
KDA convolution 使用 BF16，KDA recurrent state 使用 FP32。不加载模型权重，不执行 GPU/NPU
算子。该 profile 计算一份全模型逻辑载荷，不计 allocator padding、索引、权重、临时张量或
speculative decoding 的额外状态。

## 来源与布局

模型参数固定到官方
[config.json，revision f831ab66814297da540d832a5235f8e904f29d06](https://huggingface.co/moonshotai/Kimi-K3/blob/f831ab66814297da540d832a5235f8e904f29d06/config.json)。
该配置有 93 层：24 个 FULL/MLA 层、69 个 LINEAR/KDA 层。KDA 有 96 heads，head dimension
为 128，convolution kernel size 为 4；MLA latent dimension 为 512，额外 key dimension 为 64。

缓存实现固定到 vLLM
`ffcabb8c7ab3e066e1c77b55d7673e5d08cf26cc`：

- [Kimi-K3 KDA 的 get_state_dtype/get_state_shape](https://github.com/vllm-project/vllm/blob/ffcabb8c7ab3e066e1c77b55d7673e5d08cf26cc/vllm/models/kimi_k3/nvidia/kda.py#L571)
  调用公共 KDA state calculator。
- [KDA dtype calculator](https://github.com/vllm-project/vllm/blob/ffcabb8c7ab3e066e1c77b55d7673e5d08cf26cc/vllm/model_executor/layers/mamba/mamba_utils.py#L133)
  在 `mamba_ssm_cache_dtype="auto"` 时令 recurrent state 为 FP32；convolution dtype 随
  `mamba_cache_dtype`/model dtype。本 profile 固定 BF16 convolution。
- [KDA shape calculator](https://github.com/vllm-project/vllm/blob/ffcabb8c7ab3e066e1c77b55d7673e5d08cf26cc/vllm/model_executor/layers/mamba/mamba_utils.py#L296)
  在 TP=1、`num_spec=0` 时返回 convolution `[3, 36864]` 和 recurrent `[96, 128, 128]`。
  其中 36864 为 Q/K/V 三组 `96 * 128` 通道，history 长度为 `kernel_size - 1 = 3`。
  convolution 的 SD/DS 转置不改变字节数。
- [Kimi-K3 MLA get_kv_cache_spec](https://github.com/vllm-project/vllm/blob/ffcabb8c7ab3e066e1c77b55d7673e5d08cf26cc/vllm/models/kimi_k3/nvidia/mla.py#L421)
  使用一个 `512 + 64 = 576` 元素的 latent vector，没有另存一份展开的 V。本 profile 选 BF16
  cache；不使用 FP8 或 `fp8_ds_mla` 的量化格式。

官方 Hugging Face
[reference forward](https://huggingface.co/moonshotai/Kimi-K3/blob/f831ab66814297da540d832a5235f8e904f29d06/modeling_kimi_linear.py#L426)
先展开 K/V 再更新 reference cache。因此这里的压缩 MLA 字节数来自上述 vLLM 服务实现，不能
用来表示 Hugging Face reference cache 的实际内存占用。

## 载荷算术

| 对象 | 全模型逻辑载荷公式 | 字节数 |
|---|---|---:|
| 一个 token 的 FULL | `24 * (512 + 64) * 2` | 27,648 |
| 16-token FULL block | `16 * 27,648` | 442,368 |
| 64-token FULL block | `64 * 27,648` | 1,769,472 |
| 一个 checkpoint 的 recurrent state | `69 * 96 * 128 * 128 * 4` | 434,110,464 |
| 一个 checkpoint 的 convolution state | `69 * 3 * (3 * 96 * 128) * 2` | 15,261,696 |
| 完整 LINEAR checkpoint | `434,110,464 + 15,261,696` | 449,372,160 |

`tokens_per_block` 决定每个输入 hash 覆盖多少 token，并按比例改变 `full_block_bytes`。
它不改变 `linear_checkpoint_bytes`：每个 LINEAR checkpoint 都是完整状态，不能把它除以
tokens per block。16-token FULL block 下，LINEAR/FULL 对象大小比约为 1015.83。

这些数值是一份全模型、无复制的逻辑 payload。它们不是任意 TP 配置下单卡的实际占用，也
不能统一除以 TP：KDA head/state 可以按 TP 分片，而 MLA spec 的 latent KV 只有一个 head，
默认 TP 路径可在各 rank 复制；DCP 又有独立的数据分片语义。跨 rank 导出时的去重、复制、
padding 和传输格式必须单独测量。模拟容量与 payload 必须使用同一口径。

## 配置与输入

`tokens_per_block` 必须显式提供，并与 trace 的 hash block 大小一致。profile 不下载数据集，
不把不同块大小的数据自动重分块，不从请求长度伪造 hash。`hybrid_config()` 默认使用共享
DRAM：总容量由 `memory.capacity_bytes` 指定，两类独立配额保持 0。默认
`full_policy="lru_k"`、`linear_policy="lru_k"`、`lru_k=2`，关闭 I/O trace。

```python
from pathlib import Path

from dwpdsim import (
    DWPDSimulator,
    HybridPrefillRequest,
    MemoryConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
    kimi_k3_profile,
)

GIB = 1024**3
profile = kimi_k3_profile(tokens_per_block=64)
hybrid = profile.hybrid_config()
config = SimulationConfig(
    memory=MemoryConfig(4 * GIB),
    slc=StorageTierConfig(4096, 1),
    tlc=StorageTierConfig(4096, 1),
    block_size_bytes=4096,
    storage_policy=StoragePolicyConfig(kind="infinite_storage"),
    hybrid_attention=hybrid,
)
output = Path("build/kimi-k3")
output.mkdir(parents=True, exist_ok=True)
with DWPDSimulator(config, output) as simulator:
    simulator.process_hybrid_prefill(
        HybridPrefillRequest(0, 0, 0, [11, 22], input_tokens=129)
    )
```

示例中的 SLC/TLC 配置用于满足外层配置结构；`infinite_storage` 不模拟有限 SSD 容量或内部
GC。本例不是端到端设备寿命测试。FULL 和 LINEAR 共同使用 4 GiB DRAM，不预留类型配额。
共享模式支持两类均为 `lru_k`，或 FULL 的 `segment_lru/indexed_segment_lru` 配合 LINEAR 的
`checkpoint_lru`；不支持的混合排序组合明确报错。要输出逻辑 I/O，可设置 `emit_io_trace=True`。

`pool_allocation="fixed"` 及两个正的独立配额用于复现历史固定分区实验；`"adaptive"` 按需求
量更新软目标。二者均需显式选择。共享容量和全局 LRU-K 是当前基线，尚未实现 Gandalf 的
Drop/Move 代价估计及准入算法，见 [共享 DRAM 验证](../report/shared-dram-correction.md)。

helper 使用 `replay_mode="prefix_replay"` 与 `checkpoint_generation="request_end"`：
每个请求只在输入最后一个完整 hash block 处发布 LINEAR checkpoint，不自动生成中间
checkpoint，不根据输出 token 数推导 decode checkpoint。`input_tokens=129` 加两个
64-token hash 表示 128 个可命名 token 加一个未成块的输入尾部；发布边界为 128。
真实 decode 产生的状态及输出 block 必须由具有明确身份的输入事件提供。

## 恢复命中与验证范围

一次真正的恢复命中要求：同一 hash 路径上从根开始的 FULL `[0, r)` 连续完整可读，且边界
`r` 的 LINEAR checkpoint 完整可读。恢复位置是同时满足这两个条件的最深边界，不能跨过
FULL 中间缺口，也不能拼接不同路径的 FULL 与 LINEAR。组件可以来自 DRAM 或 SSD；
`component hit` 只用于诊断，不能将 FULL hit 与 LINEAR hit 单独相加作为请求命中。

profile 的单元测试验证载荷算术、块大小变化和配置构造；模拟器的混合恢复测试验证上述
FULL/LINEAR 交集。不执行 NPU/GPU 模型，不证明真实引擎已生成这些 checkpoint，也不将
逻辑 I/O 计数或容量结果作为真实 TTFT、SSD WAF 或寿命实测。

## LRU-K 基线

FULL block 与 LINEAR checkpoint 各自使用独立索引，在共享模式下使用同一请求与准入时钟，
比较两类队首，等价于对带组件身份的全部驻留对象做全局排序。`lru_k` 是正整数。每个对象记录最近 K
个实际消费它的请求序号；一次请求最多记录一次。先淘汰不足 K 次引用的冷对象；零引用对象
按进入 DRAM 的先后淘汰，其他冷对象按最近实际消费的 LRU 排序。成熟对象选择第 K 次最近
引用最早的对象，平手再比较最近引用、入池顺序和 hash。K=1 因而按实际 backend 需求退化
为 LRU。对象大小决定需要释放多少字节，不改变 LRU-K 排序。

发布或重复发布 checkpoint 不等于实际复用，不增加引用次数。已有 SSD 副本的对象释放
DRAM 后保留引用历史，重新读入时继续累计；FULL 与 LINEAR 同 hash 也不共享引用历史。
前缀查询只更新真正消费的 LINEAR checkpoint，其祖先或被跳过的 checkpoint 不增加引用。
这是一份 backend 对象需求的 LRU-K 基线，不是论文中在某次数门槛后才准入 SSD 的频率过滤。

## 无 trace 回放

`emit_io_trace=False` 时不创建任何 I/O trace 文件，仍逐对象累计逻辑读写量、按存储粒度
取整的分配字节、SSD 副本存在性及联合恢复指标。此模式只保存分配单元数，不建立地址列表；
`hybrid_inspect()` 的 `storage_addresses` 为空，但 `on_storage` 与 `storage_allocated_bytes`
仍表示实际逻辑副本状态。打开 trace 时才分配并输出地址。

`run_hybrid_prefill()` 按批次提交到 C++ 聚合入口，跳过逐请求 FULL 来源列表和 checkpoint
输出列表。`process_hybrid_prefill()` 保留逐请求恢复详情。两者使用相同恢复、淘汰和发布流程，
累计指标必须完全一致。

主指标 `prefill.joint_token_hit_rate = restored_tokens / input_tokens`，只有完整的 FULL/LINEAR
交集才能贡献 `restored_tokens`；`joint_restored_request_rate` 统计至少恢复了一个有效前缀的
请求比例，不代表整条输入零计算。组件 `dram_hits/storage_hits` 仅作诊断。
精确对象模式中，单独的 FULL 或 LINEAR 探测也不会令 `all_available` 或 `all_dram` 为真；
只有 `components="both"` 且该恢复边界全部组件可读才计为真正命中。

```bash
.venv/bin/python benchmark/kimi_no_trace.py --requests 10000 --rounds 3
```

基准固定随机种子、真实 Kimi-K3 字节数和持续增长的多会话前缀，每轮独立进程、空缓存启动。
该性能脚本显式使用历史 fixed 分区以保持前后版本可比；共享 DRAM 的 WEKA 基线见上述验证报告。
计时包含 batch 的 pybind 封送与 C++ replay，排除输入生成、Python 请求对象构造及最终统计
序列化；RSS 包括预构造输入和元数据，不分配真实 KV payload。该结果衡量模拟器吞吐，
不是 Kimi-K3 推理吞吐。
