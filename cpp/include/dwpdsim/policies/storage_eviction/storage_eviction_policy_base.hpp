#pragma once

#include <optional>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

class RadixTree;

class StorageEvictionPolicyBase {
  public:
    virtual ~StorageEvictionPolicyBase() = default;

    virtual NodeId choose_victim(
        Medium medium,
        NodeId incoming_node,
        const AccessContext& context,
        const RadixTree& tree
    ) = 0;

    virtual StorageEvictionAction eviction_action(
        Medium medium,
        NodeId victim_endpoint,
        NodeId incoming_node,
        const AccessContext& context,
        const RadixTree& tree
    ) = 0;

    virtual void on_storage_read(NodeId node_id, Medium medium) = 0;
    virtual void on_storage_write(NodeId node_id, Medium medium) = 0;
    virtual void on_storage_remove(NodeId node_id, Medium medium) = 0;

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

using StorageEvictionPolicy = StorageEvictionPolicyBase;

}  // namespace dwpdsim
