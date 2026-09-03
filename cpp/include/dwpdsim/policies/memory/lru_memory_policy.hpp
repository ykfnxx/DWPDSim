#pragma once

#include <optional>
#include <unordered_map>

#include "dwpdsim/policies/memory/memory_policy_base.hpp"

namespace dwpdsim {

class LruMemoryPolicy final : public MemoryPolicyBase {
  public:
    explicit LruMemoryPolicy(
        bool admit_storage_hits = true,
        EvictionAction eviction_action = EvictionAction::Persist
    );

    bool admit_storage_hit(
        const AccessContext& context,
        const Node& node,
        const RadixTree& tree
    ) override;
    NodeId choose_victim(
        const AccessContext& context,
        const RadixTree& tree
    ) override;
    EvictionAction eviction_action(
        const Node& victim,
        const AccessContext& context,
        const RadixTree& tree
    ) override;

    void on_memory_insert(NodeId node_id) override;
    void on_memory_access(NodeId node_id) override;
    void on_memory_remove(NodeId node_id) override;

  private:
    struct Link {
        std::optional<NodeId> previous;
        std::optional<NodeId> next;
    };

    void attach_front(NodeId node_id) noexcept;
    void detach(NodeId node_id) noexcept;

    bool admit_storage_hits_;
    EvictionAction eviction_action_;
    std::unordered_map<NodeId, Link> links_;
    std::optional<NodeId> head_;
    std::optional<NodeId> tail_;
};

}  // namespace dwpdsim
