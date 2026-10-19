#pragma once

#include <array>
#include <fstream>
#include <optional>
#include <set>
#include <unordered_map>
#include <vector>

#include "dwpdsim/hybrid_types.hpp"
#include "dwpdsim/radix_tree.hpp"

namespace dwpdsim {

// Derived policy indices only. All payload/residency authority remains on RadixTree nodes.
class HybridMemoryIndex {
  public:
    using Key = std::pair<std::uint64_t, NodeId>;
    void touch(std::size_t kind, NodeId id);
    void remove(std::size_t kind, NodeId id);
    std::vector<NodeId> victim(std::size_t kind, const RadixTree& tree,
        const HybridAttentionConfig& config, TimestampNs now, std::uint64_t& examined) const;
  private:
    std::array<std::set<Key>, 2> lru_;
    std::array<std::unordered_map<NodeId, std::uint64_t>, 2> keys_;
    std::uint64_t recency_ = 0;
};

struct HybridRuntime {
    HybridMemoryIndex index;
    std::array<HybridCounters, 2> counters;
    std::optional<std::uint64_t> last_sequence;
    std::uint64_t requests = 0, all_available_requests = 0, all_dram_requests = 0;
    std::uint64_t joint_requests = 0, joint_available = 0, joint_dram = 0;
    std::uint64_t io_sequence = 0;
    std::ofstream io;
};

} // namespace dwpdsim
