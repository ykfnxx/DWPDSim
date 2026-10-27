#pragma once

#include <cstdint>
#include <optional>
#include <vector>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

class RadixTree;

struct MemoryEvictionDecision {
    NodeId leaf_segment_endpoint;
    MemoryEvictionAction action;
    bool reclaim_parent = true;
    std::optional<std::uint64_t> max_eviction_blocks;
};

enum class MemoryMutationKind : std::uint8_t {
    Inserted,
    Accessed,
    Removed,
};

struct MemoryMutation {
    MemoryMutationKind kind;
    NodeId node_id;
};

struct MemoryPolicyWork {
    std::uint64_t candidates_examined = 0;
    std::uint64_t topology_nodes_examined = 0;
    std::uint64_t topology_member_moves = 0;
    std::uint64_t ancestor_updates = 0;
    std::uint64_t worker_rounds = 0;
    std::uint64_t indexed_segments = 0;
    std::uint64_t indexed_residents = 0;
    std::uint64_t ancestor_entries = 0;
    // reuse_lru completed Memory spells, upper edges 1,4,16,60,300,1800,7200,inf seconds.
    std::vector<std::uint64_t> evicted_idle_bins;
    std::vector<std::uint64_t> evicted_residency_bins;
};

class MemoryPolicy {
  public:
    virtual ~MemoryPolicy() = default;
    virtual void on_request_begin(const RequestContext&, const RadixTree&) {}
    virtual void on_request_end(const RequestContext&, const RadixTree&) {}
    virtual void bind_tree(const RadixTree&) {}
    virtual void on_node_created(NodeId, const RadixTree&) {}
    virtual void on_node_pruned(NodeId, std::optional<NodeId>, const RadixTree&) {}
    virtual MemoryPolicyWork work() const { return {}; }

    virtual bool admit_storage_hit(
        const AccessContext& access,
        const Node& node,
        const RadixTree& tree
    ) const = 0;
    virtual MemoryEvictionDecision evict(
        const RequestContext& request,
        const RadixTree& tree
    ) const = 0;
    virtual void on_commit(const MemoryMutation& mutation) = 0;
};

}  // namespace dwpdsim
