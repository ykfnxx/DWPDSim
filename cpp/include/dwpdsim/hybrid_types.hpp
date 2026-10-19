#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace dwpdsim {

// Optional per-node data: allocated only by the explicitly enabled hybrid executor.
struct HybridComponent {
    std::uint64_t size_bytes = 0;
    bool published = false;
    bool in_dram = false;
    std::vector<std::uint64_t> storage_addresses;
    std::uint64_t demands = 0;
    std::uint64_t last_demand_ns = 0;
    double frequency = 0.0;
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
    double alpha = 0.1;
    double decay_seconds = 60.0;
    double linear_read_cost = 1.0;
    bool admit_storage_hits = true;
    bool dump_on_eviction = true;
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
};

} // namespace dwpdsim
