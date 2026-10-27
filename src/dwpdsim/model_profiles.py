"""Logical KV payload sizes for the pinned Kimi-K3 serving layout."""

from dataclasses import dataclass
from typing import ClassVar

from dwpdsim.config import HybridAttentionConfig


@dataclass(frozen=True, slots=True)
class KimiK3Profile:
    """Whole-model BF16 MLA/conv and FP32 KDA state, without allocator padding.

    The profile uses compressed MLA as implemented by vLLM, not the expanded
    key/value cache in the Hugging Face reference forward. It describes one
    logical copy of every layer, with no speculative-decoding state.
    """

    tokens_per_block: int

    model_id: ClassVar[str] = "moonshotai/Kimi-K3"
    config_revision: ClassVar[str] = "f831ab66814297da540d832a5235f8e904f29d06"
    cache_implementation_revision: ClassVar[str] = "ffcabb8c7ab3e066e1c77b55d7673e5d08cf26cc"
    full_layers: ClassVar[int] = 24
    linear_layers: ClassVar[int] = 69
    kda_heads: ClassVar[int] = 96
    kda_head_dim: ClassVar[int] = 128
    convolution_kernel_size: ClassVar[int] = 4
    mla_latent_dim: ClassVar[int] = 512
    mla_key_extra_dim: ClassVar[int] = 64
    full_dtype: ClassVar[str] = "bfloat16"
    convolution_dtype: ClassVar[str] = "bfloat16"
    recurrent_dtype: ClassVar[str] = "float32"

    def __post_init__(self) -> None:
        if type(self.tokens_per_block) is not int or self.tokens_per_block <= 0:
            raise ValueError("tokens_per_block must be a positive integer")

    @property
    def full_bytes_per_token(self) -> int:
        """One compressed 576-element BF16 vector per FULL layer."""
        return self.full_layers * (self.mla_latent_dim + self.mla_key_extra_dim) * 2

    @property
    def full_block_bytes(self) -> int:
        return self.tokens_per_block * self.full_bytes_per_token

    @property
    def recurrent_state_bytes(self) -> int:
        """All KDA matrices: 69 layers times [96, 128, 128] FP32."""
        return self.linear_layers * self.kda_heads * self.kda_head_dim**2 * 4

    @property
    def convolution_state_bytes(self) -> int:
        """All Q/K/V histories: 69 layers times [3, 3*96*128] BF16."""
        return (
            self.linear_layers
            * (self.convolution_kernel_size - 1)
            * 3
            * self.kda_heads
            * self.kda_head_dim
            * 2
        )

    @property
    def linear_checkpoint_bytes(self) -> int:
        """A complete recurrent and convolution snapshot, independent of block size."""
        return self.recurrent_state_bytes + self.convolution_state_bytes

    def hybrid_config(
        self,
        *,
        pool_allocation: str = "shared",
        full_capacity_bytes: int = 0,
        linear_capacity_bytes: int = 0,
        full_policy: str = "lru_k",
        linear_policy: str = "lru_k",
        lru_k: int = 2,
        emit_io_trace: bool = False,
    ) -> HybridAttentionConfig:
        """Construct request-end replay with shared DRAM by default.

        A checkpoint covers the last complete input block of each request.
        Generated output tokens are not inferred from the input hash path.
        """
        return HybridAttentionConfig(
            enabled=True,
            pool_allocation=pool_allocation,
            full_capacity_bytes=full_capacity_bytes,
            linear_capacity_bytes=linear_capacity_bytes,
            full_block_bytes=self.full_block_bytes,
            linear_checkpoint_bytes=self.linear_checkpoint_bytes,
            full_policy=full_policy,
            linear_policy=linear_policy,
            lru_k=lru_k,
            replay_mode="prefix_replay",
            tokens_per_block=self.tokens_per_block,
            checkpoint_interval_tokens=self.tokens_per_block,
            checkpoint_generation="request_end",
            emit_io_trace=emit_io_trace,
        )


def kimi_k3_profile(*, tokens_per_block: int) -> KimiK3Profile:
    """Select the pinned Kimi-K3 payload layout with an explicit hash-block size."""
    return KimiK3Profile(tokens_per_block=tokens_per_block)
