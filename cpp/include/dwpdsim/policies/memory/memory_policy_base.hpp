#pragma once

#include <optional>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

class RadixTree;

class MemoryPolicyBase {
  public:
    virtual ~MemoryPolicyBase() = default;

    virtual bool admit_storage_hit(
        const AccessContext& context,
        const Node& node,
        const RadixTree& tree
    ) = 0;
    virtual NodeId choose_victim(
        const AccessContext& context,
        const RadixTree& tree
    ) = 0;
    virtual EvictionAction eviction_action(
        const Node& victim,
        const AccessContext& context,
        const RadixTree& tree
    ) = 0;

    virtual void on_memory_insert(NodeId node_id) = 0;
    virtual void on_memory_access(NodeId node_id) = 0;
    virtual void on_memory_remove(NodeId node_id) = 0;

    virtual void on_node_created(
        NodeId,
        std::optional<NodeId>,
        const RadixTree&
    ) {}
    virtual void on_node_removed(
        NodeId,
        std::optional<NodeId>,
        const RadixTree&
    ) {}
    virtual void on_access_complete(
        const AccessContext&,
        AccessResult,
        const RadixTree&
    ) {}
};

using MemoryPolicy = MemoryPolicyBase;

}  // namespace dwpdsim
