#pragma once

#include <array>
#include <fstream>
#include <optional>
#include <set>
#include <tuple>
#include <unordered_map>
#include <vector>

#include "dwpdsim/hybrid_types.hpp"
#include "dwpdsim/policies/memory/joint_cost.hpp"
#include "dwpdsim/policies/memory/indexed_memory_lru_policy.hpp"
#include "dwpdsim/radix_tree.hpp"

namespace dwpdsim {

struct HybridEvictionDecision {
    std::vector<NodeId> victims;
    bool drop = false;
    std::size_t kind = 0;
};

// Reuse the existing derived segment index; it never reads legacy residency.
class HybridFullLruIndex final : public IndexedMemoryLruPolicy {
  public:
    HybridFullLruIndex() : IndexedMemoryLruPolicy(true, 1, 0, 1, 0) {}
    NodeId victim() const { return select_victim(); }
};

// Derived policy indices only. All payload/residency authority remains on RadixTree nodes.
class HybridMemoryIndex {
  public:
    using Key = std::pair<std::uint64_t, NodeId>;
    void bind(const RadixTree& tree, const HybridAttentionConfig& config);
    void on_node_created(NodeId id, const RadixTree& tree);
    // One clock tick per request; only actual component demands record references.
    void begin_request();
    void reference(std::size_t kind, NodeId id);
    void touch(std::size_t kind, NodeId id);
    void remove(std::size_t kind, NodeId id);
    std::vector<NodeId> victim(std::size_t kind, const RadixTree& tree,
        const HybridAttentionConfig& config, TimestampNs now, std::uint64_t& examined,
        std::optional<NodeId> incoming = std::nullopt, bool drop_next = false,
        std::uint64_t linear_budget = 0) const;
    HybridEvictionDecision decide(std::size_t kind, const RadixTree& tree,
        const HybridAttentionConfig& config, TimestampNs now, std::uint64_t& examined,
        std::optional<NodeId> incoming, bool legacy_drop, std::uint64_t linear_budget) const;
    HybridEvictionDecision decide_shared(const RadixTree& tree,
        const HybridAttentionConfig& config, TimestampNs now,
        std::array<HybridCounters, 2>& counters, const std::array<bool, 2>& legacy_drop,
        std::size_t incoming_kind, NodeId incoming, std::uint64_t capacity) const;
    std::vector<std::vector<HybridCostObject>> cost_candidates(const RadixTree& tree,
        std::uint64_t required_bytes, std::size_t groups) const;
  private:
    struct ReferenceHistory {
        std::vector<std::uint64_t> requests;
        std::size_t next = 0;
        std::uint64_t last_request = 0;
    };
    // Cold: LRU of actual demands, with unreferenced objects ordered by admission.
    // Mature: oldest Kth reference, then last reference, admission and logical id.
    using LruKKey = std::tuple<bool, std::uint64_t, std::uint64_t, std::uint64_t, NodeId>;
    LruKKey lru_k_key(std::size_t kind, NodeId id, std::uint64_t admission) const;

    std::array<std::set<Key>, 2> lru_;
    std::array<std::unordered_map<NodeId, std::uint64_t>, 2> keys_;
    std::array<std::set<LruKKey>, 2> lru_k_order_;
    std::array<std::unordered_map<NodeId, ReferenceHistory>, 2> reference_history_;
    std::array<bool, 2> use_lru_k_{};
    std::size_t lru_k_ = 1;
    std::uint64_t request_clock_ = 0;
    std::uint64_t recency_ = 0;
    std::unique_ptr<HybridFullLruIndex> full_;
    const RadixTree* tree_ = nullptr;
    bool frontier_ = false;
    bool track_descendants_ = false;
    std::unordered_map<NodeId, std::uint64_t> linear_descendants_;
};

struct HybridRuntime {
    HybridMemoryIndex index;
    std::unique_ptr<JointMemoryCostPolicy> cost;
    std::array<HybridCounters, 2> counters;
    HybridPrefillCounters prefill;
    std::optional<std::uint64_t> last_sequence;
    std::uint64_t requests = 0, all_available_requests = 0, all_dram_requests = 0;
    std::uint64_t joint_requests = 0, joint_available = 0, joint_dram = 0;
    std::uint64_t io_sequence = 0;
    std::ofstream io;
    std::array<long double, 2> demand_mass{};
    std::array<std::uint64_t, 2> target_bytes{};
    std::optional<TimestampNs> last_pool_update;
    std::uint64_t pool_updates = 0, cross_pool_reclaims = 0, peak_resident_bytes = 0;
    std::uint64_t current_request_id = 0, current_affinity_id = 0;
    std::array<std::unordered_map<NodeId, std::uint64_t>, 2> read_sequences;
    struct TierCounters {
        std::uint64_t reads = 0, writes = 0, trims = 0;
        std::uint64_t read_bytes = 0, write_bytes = 0, trim_bytes = 0;
        std::uint64_t capacity_trims = 0, idle_trims = 0, migrated_objects = 0;
    };
    std::array<TierCounters, 2> storage_counters;
};

} // namespace dwpdsim
