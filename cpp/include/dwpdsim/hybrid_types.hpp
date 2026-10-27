#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace dwpdsim {

// Optional per-node data: allocated only by the explicitly enabled hybrid executor.
struct HybridComponent {
    std::uint64_t size_bytes = 0;
    bool published = false;
    bool in_dram = false;
    std::vector<std::uint64_t> storage_addresses;
    std::uint64_t storage_units = 0;
    bool on_storage() const noexcept { return storage_units != 0; }
    std::uint64_t demands = 0;
    std::uint64_t last_demand_ns = 0;
    std::uint64_t last_touch_ns = 0; // Admission or actual consumption; retention clock.
    double frequency = 0.0;
    // Prefix opportunities are an estimator, separate from consumed-checkpoint demand.
    double prefix_frequency = 0.0;
    std::uint64_t last_prefix_ns = 0, checkpoint_tokens = 0;
    double target_frequency = 0.0;
    std::uint64_t last_target_ns = 0;
    std::optional<std::uint64_t> previous_checkpoint;
};

struct HybridNodeState {
    std::array<HybridComponent, 2> components;
};

// Preserve Node/RadixTree value semantics while keeping disabled-mode state
// unallocated. A const tree view must not permit changing component residency.
class HybridNodeStorage {
  public:
    HybridNodeStorage() = default;
    HybridNodeStorage(const HybridNodeStorage& other)
        : state_(other.state_ ? std::make_unique<HybridNodeState>(*other.state_) : nullptr) {}
    HybridNodeStorage& operator=(const HybridNodeStorage& other) {
        state_ = other.state_ ? std::make_unique<HybridNodeState>(*other.state_) : nullptr;
        return *this;
    }
    HybridNodeStorage(HybridNodeStorage&&) noexcept = default;
    HybridNodeStorage& operator=(HybridNodeStorage&&) noexcept = default;

    explicit operator bool() const noexcept { return static_cast<bool>(state_); }
    HybridNodeState* operator->() noexcept { return state_.get(); }
    const HybridNodeState* operator->() const noexcept { return state_.get(); }
    HybridNodeState& ensure() {
        if (!state_) { state_ = std::make_unique<HybridNodeState>(); }
        return *state_;
    }

  private:
    std::unique_ptr<HybridNodeState> state_;
};

struct HybridAttentionConfig {
    bool enabled = false;
    std::uint64_t full_capacity_bytes = 0;
    std::uint64_t linear_capacity_bytes = 0;
    std::uint64_t full_block_bytes = 0;
    std::uint64_t linear_checkpoint_bytes = 0;
    std::string full_policy = "segment_lru";
    std::string linear_policy = "checkpoint_lru";
    std::uint64_t lru_k = 2;
    double alpha = 0.1;
    double decay_seconds = 60.0;
    double linear_read_cost = 1.0;
    bool admit_storage_hits = true;
    bool dump_on_eviction = true;
    std::string replay_mode = "exact_objects";
    std::uint64_t tokens_per_block = 0;
    std::uint64_t checkpoint_interval_tokens = 4096;
    std::optional<double> full_drop_fraction;
    std::optional<double> linear_drop_fraction;
    bool emit_io_trace = true;
    double recompute_weight = 1.0;
    double tail_prior = 1.0;
    double drop_phase = 0.0;
    std::string pool_allocation = "shared";
    double pool_decay_seconds = 300.0;
    std::string eviction_mode = "legacy";
    std::optional<std::uint64_t> full_retention_ns, linear_retention_ns;
    std::string linear_retention_policy = "idle";
    std::string checkpoint_generation = "chunked_prefill";
};

struct HybridPrefillRequest {
    std::uint64_t timestamp_ns = 0;
    std::uint64_t sequence = 0;
    std::uint64_t request_id = 0;
    std::vector<std::uint64_t> hash_ids;
    std::uint64_t input_tokens = 0; // 0 infers complete blocks only.
    std::optional<std::uint64_t> max_prefix_tokens;
    std::uint64_t affinity_id = 0;
};

struct HybridPrefillResult {
    std::uint64_t sequence = 0, request_id = 0, input_tokens = 0;
    std::uint64_t matched_prefix_tokens = 0, full_available_prefix_tokens = 0;
    std::uint64_t restore_tokens = 0;
    std::optional<std::uint64_t> restore_hash_id;
    std::vector<std::string> full;
    std::string linear = "initial";
    std::uint64_t prefill_compute_tokens = 0, replayed_prefix_tokens = 0, new_suffix_tokens = 0;
    std::vector<std::uint64_t> generated_checkpoint_tokens;
};

struct HybridPrefillCounters {
    std::uint64_t restored_requests = 0, dram_restored_requests = 0;
    std::uint64_t input_tokens = 0, matched_prefix_tokens = 0, restored_tokens = 0;
    std::uint64_t prefill_compute_tokens = 0, replayed_prefix_tokens = 0, new_suffix_tokens = 0;
    std::uint64_t generated_checkpoints = 0;
};

struct HybridEvent {
    std::uint64_t timestamp_ns = 0;
    std::uint64_t sequence = 0;
    std::string action = "request";
    std::vector<std::uint64_t> hash_ids;
    std::uint64_t request_id = 0;
    std::string components = "both";
    std::uint64_t restore_blocks = 0; // 0 means the complete input path.
    std::uint64_t size_bytes = 0; // Publish: 0 means the configured component size.
};

struct HybridResult {
    std::uint64_t sequence = 0;
    std::uint64_t request_id = 0;
    std::uint64_t restore_blocks = 0;
    std::vector<std::string> full;
    std::string linear = "not_requested";
    bool all_available = false;
    bool all_dram = false;
};

struct HybridCounters {
    std::uint64_t demands = 0, dram_hits = 0, storage_hits = 0;
    std::uint64_t never_published_misses = 0, lost_misses = 0;
    std::uint64_t publications = 0, published_bytes = 0;
    std::uint64_t resident_bytes = 0, peak_resident_bytes = 0, resident_objects = 0;
    std::uint64_t storage_bytes = 0, storage_allocated_bytes = 0;
    std::uint64_t read_objects = 0, read_bytes = 0, read_allocated_bytes = 0;
    std::uint64_t write_objects = 0, write_bytes = 0, write_allocated_bytes = 0;
    std::uint64_t evictions = 0, bypasses = 0, candidates_examined = 0;
    std::uint64_t eligible_evictions = 0, drop_objects = 0, dump_objects = 0, backed_evictions = 0;
    std::uint64_t evicted_bytes = 0, eligible_bytes = 0, lost_bytes = 0, dump_bytes = 0, backed_bytes = 0;
    std::uint64_t drop_action_objects = 0, drop_action_bytes = 0;
    std::uint64_t trim_objects = 0, trim_bytes = 0, admission_rejections = 0;
};

} // namespace dwpdsim
