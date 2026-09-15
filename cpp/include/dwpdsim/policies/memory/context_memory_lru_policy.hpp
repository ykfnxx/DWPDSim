#pragma once

#include "dwpdsim/policies/memory/indexed_memory_lru_policy.hpp"

namespace dwpdsim {

// Prefer shallow endpoints within a capacity-bounded, globally oldest candidate set.
class ContextMemoryLruPolicy final : public IndexedMemoryLruPolicy {
  public:
    ContextMemoryLruPolicy(bool admit_storage_hits, std::uint64_t capacity_blocks,
                           double alpha = 0.01,
                           std::optional<TimestampNs> retention_ns = std::nullopt,
                           std::optional<std::uint64_t> max_eviction_blocks = std::nullopt,
                           double retention_growth_seconds_per_block = 0.0,
                           std::optional<TimestampNs> eviction_gap_reference_ns = std::nullopt,
                           std::uint64_t eviction_base_blocks = 64);
    MemoryEvictionDecision evict(const RequestContext&, const RadixTree&) const override;

    void on_request_begin(const RequestContext&, const RadixTree&) override;
    void on_request_end(const RequestContext&, const RadixTree&) override;
    void on_commit(const MemoryMutation&) override;
    void on_node_pruned(NodeId, std::optional<NodeId>, const RadixTree&) override;

  private:
    struct SessionHistory {
        std::uint64_t context_blocks;
        std::uint64_t growth_blocks;
    };
    struct AccessHistory {
        RequestId request_id;
        TimestampNs timestamp_ns;
        std::optional<TimestampNs> gap_ns;
        std::uint64_t growth_blocks;
    };
    NodeId select_victim() const override;
    double candidate_block_budget_;
    std::optional<std::uint64_t> max_eviction_blocks_;
    std::optional<TimestampNs> retention_ns_;
    double retention_growth_seconds_per_block_;
    std::optional<TimestampNs> eviction_gap_reference_ns_;
    std::uint64_t eviction_base_blocks_;
    bool adaptive_;
    RequestId active_request_id_ = 0;
    TimestampNs active_timestamp_ns_ = 0;
    std::uint64_t active_growth_blocks_ = 0;
    std::unordered_map<AffinityId, SessionHistory> sessions_;
    std::unordered_map<NodeId, AccessHistory> access_history_;
};

}  // namespace dwpdsim
