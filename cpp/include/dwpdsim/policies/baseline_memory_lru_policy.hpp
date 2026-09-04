#pragma once

#include <optional>
#include <unordered_map>

#include "dwpdsim/policies/memory_policy.hpp"

namespace dwpdsim {

class BaselineMemoryLruPolicy final : public MemoryPolicy {
  public:
    explicit BaselineMemoryLruPolicy(bool admit_storage_hits = true);

    bool admit_storage_hit(
        const AccessContext& access,
        const Node& node,
        const RadixTree& tree
    ) const override;
    MemoryEvictionDecision evict(
        const RequestContext& request,
        const RadixTree& tree
    ) const override;
    void on_commit(const MemoryMutation& mutation) override;

  private:
    struct Link {
        std::optional<NodeId> previous;
        std::optional<NodeId> next;
    };

    void attach_front(NodeId node_id);
    void detach(NodeId node_id);

    bool admit_storage_hits_;
    std::unordered_map<NodeId, Link> links_;
    std::optional<NodeId> head_;
    std::optional<NodeId> tail_;
};

}  // namespace dwpdsim
