"""Pinned whole-model payload accounting and public replay configuration."""

from dwpdsim.model_profiles import KimiK3Profile, kimi_k3_profile


def test_kimi_k3_whole_model_payloads():
    profile = kimi_k3_profile(tokens_per_block=64)
    assert profile.config_revision == "f831ab66814297da540d832a5235f8e904f29d06"
    assert profile.full_layers + profile.linear_layers == 93
    assert profile.full_bytes_per_token == 27_648
    assert profile.full_block_bytes == 1_769_472
    assert profile.recurrent_state_bytes == 434_110_464
    assert profile.convolution_state_bytes == 15_261_696
    assert profile.linear_checkpoint_bytes == 449_372_160


def test_hash_block_size_changes_full_payload_and_preserves_linear_snapshot():
    small = KimiK3Profile(tokens_per_block=16)
    large = KimiK3Profile(tokens_per_block=64)
    assert small.full_block_bytes == 442_368
    assert large.full_block_bytes == 4 * small.full_block_bytes
    assert small.linear_checkpoint_bytes == large.linear_checkpoint_bytes


def test_kimi_k3_constructs_reference_lru_k_replay_without_trace():
    profile = kimi_k3_profile(tokens_per_block=64)
    config = profile.hybrid_config()
    assert config.enabled
    assert (config.full_policy, config.linear_policy, config.lru_k) == ("lru_k", "lru_k", 2)
    assert config.pool_allocation == "shared"
    assert config.full_capacity_bytes == config.linear_capacity_bytes == 0
    assert config.full_block_bytes == 1_769_472
    assert config.linear_checkpoint_bytes == 449_372_160
    assert config.replay_mode == "prefix_replay"
    assert config.checkpoint_generation == "request_end"
    assert config.tokens_per_block == config.checkpoint_interval_tokens == 64
    assert not config.emit_io_trace


def test_kimi_k3_configuration_keeps_explicit_policy_and_trace_choices():
    profile = kimi_k3_profile(tokens_per_block=16)
    config = profile.hybrid_config(
        pool_allocation="fixed",
        full_capacity_bytes=profile.full_block_bytes,
        linear_capacity_bytes=profile.linear_checkpoint_bytes,
        full_policy="indexed_segment_lru",
        linear_policy="checkpoint_lru",
        lru_k=3,
        emit_io_trace=True,
    )
    assert config.full_policy == "indexed_segment_lru"
    assert config.linear_policy == "checkpoint_lru"
    assert config.lru_k == 3
    assert config.emit_io_trace
