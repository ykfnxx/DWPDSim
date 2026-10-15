#pragma once

#include "dwpdsim/policies/memory/indexed_memory_lru_policy.hpp"

namespace dwpdsim {

// Fixed, past-trained session-gap survival curves. No future request/loop labels.
class ReuseMemoryLruPolicy final : public IndexedMemoryLruPolicy {
  public:
    ReuseMemoryLruPolicy(bool admit, std::uint64_t capacity, double alpha,
        std::optional<TimestampNs> retention, std::optional<std::uint64_t> max_blocks,
        std::vector<double> edges, std::vector<std::vector<double>> survival,
        std::optional<double> dump_probability, bool priority,
        std::optional<double> admit_probability, double short_horizon, double long_horizon,
        std::vector<std::vector<std::vector<double>>> prefix_keep = {},
        bool cost_weighted = false, double recompute_weight = 1.0);
    void on_request_begin(const RequestContext&, const RadixTree&) override;
    void on_commit(const MemoryMutation&) override;
    void on_node_pruned(NodeId, std::optional<NodeId>, const RadixTree&) override;
    MemoryEvictionDecision evict(const RequestContext&, const RadixTree&) const override;
    bool admit_storage_hit(const AccessContext&, const Node&, const RadixTree&) const override;

  private:
    NodeId select_victim() const override;
    double survival(std::size_t group, double age) const;
    double probability(std::size_t group, double age, double horizon) const;
    double segment_probability(NodeId endpoint, double horizon) const;
    double segment_value(NodeId endpoint) const;
    struct Feature {
        std::size_t group, context, position;
        TimestampNs admitted_at;
    };
    double budget_;
    std::optional<std::uint64_t> max_blocks_;
    std::optional<TimestampNs> retention_for_cost_;
    std::vector<double> edges_;
    std::vector<std::vector<double>> survival_;
    std::vector<std::vector<std::vector<double>>> prefix_keep_;
    bool cost_weighted_;
    double recompute_weight_;
    std::optional<double> dump_probability_, admit_probability_;
    bool priority_, admit_;
    double short_horizon_, long_horizon_;
    TimestampNs now_ = 0;
    std::size_t active_group_ = 0;
    std::size_t active_context_ = 0;
    std::unordered_map<NodeId, std::size_t> active_positions_;
    std::unordered_map<AffinityId, TimestampNs> sessions_;
    std::unordered_map<NodeId, std::size_t> groups_by_node_;
    std::unordered_map<NodeId, Feature> features_;
    mutable std::vector<NodeId> value_scratch_;
};

}  // namespace dwpdsim
