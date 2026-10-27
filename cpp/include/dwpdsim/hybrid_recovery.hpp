#pragma once

#include <array>
#include <optional>
#include <unordered_map>
#include <vector>

#include "dwpdsim/radix_tree.hpp"

namespace dwpdsim {

// A quote describes copy changes only; it never copies or modifies the tree.
struct HybridCopyChange {
    NodeId id = 0;
    std::size_t kind = 0;
    bool remove_dram = false, remove_storage = false;
    std::optional<StorageTier> add_storage;
    bool add_dram = false;
    bool suppress_storage = false;
    bool publish = false;
    std::uint64_t size_bytes = 0;
};

struct HybridCopyView {
    bool published = false, in_dram = false;
    std::optional<StorageTier> storage;
    std::uint64_t size_bytes = 0;
    bool available() const { return published && (in_dram || storage.has_value()); }
};

class HybridRecoveryState {
  public:
    explicit HybridRecoveryState(const RadixTree& tree,
        const std::vector<HybridCopyChange>& changes = {});
    HybridCopyView component(NodeId id, std::size_t kind) const;

  private:
    const RadixTree& tree_;
    std::unordered_map<NodeId, std::array<std::optional<HybridCopyChange>, 2>> changes_;
};

struct HybridRecoveryValue {
    std::uint64_t matched_tokens = 0, full_available_tokens = 0, restored_tokens = 0;
    std::optional<NodeId> restore_id;
    std::array<std::uint64_t, 2> storage_read_bytes{};
    std::uint64_t gpu_transfer_bytes = 0;
    double load_seconds = 0, compute_seconds = 0, total_seconds = 0;
};

// Uses the executor's deepest legal endpoint. Bulk SSD reads on disjoint pools
// overlap; GPU upload follows the reads; suffix compute follows complete load.
// Queue interference and migration blocking are quoted separately.
class HybridRecoveryModel {
  public:
    HybridRecoveryModel(std::uint64_t tokens_per_block, std::uint64_t allocation_bytes,
        std::array<double, 2> storage_bytes_per_second,
        double gpu_bytes_per_second, double prefill_tokens_per_second);
    HybridRecoveryValue evaluate(const HybridPrefillRequest& request,
        const HybridRecoveryState& state) const;

  private:
    std::uint64_t tokens_per_block_, allocation_bytes_;
    std::array<double, 2> storage_bandwidth_;
    double gpu_bandwidth_, prefill_throughput_;
};

} // namespace dwpdsim
