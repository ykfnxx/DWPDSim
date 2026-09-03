#pragma once

#include <array>
#include <optional>
#include <unordered_map>

#include "dwpdsim/policies/storage_eviction/storage_eviction_policy_base.hpp"

namespace dwpdsim {

class LruStorageEvictionPolicy final : public StorageEvictionPolicyBase {
  public:
    NodeId choose_victim(
        Medium medium,
        NodeId incoming_node,
        const AccessContext& context,
        const RadixTree& tree
    ) override;

    StorageEvictionAction eviction_action(
        Medium medium,
        NodeId victim_endpoint,
        NodeId incoming_node,
        const AccessContext& context,
        const RadixTree& tree
    ) override;

    void on_storage_read(NodeId node_id, Medium medium) override;
    void on_storage_write(NodeId node_id, Medium medium) override;
    void on_storage_remove(NodeId node_id, Medium medium) override;

  private:
    struct Link {
        std::optional<NodeId> previous;
        std::optional<NodeId> next;
        Medium medium = Medium::Slc;
    };

    void attach_front(NodeId node_id, Medium medium) noexcept;
    void detach(NodeId node_id) noexcept;

    std::unordered_map<NodeId, Link> links_;
    std::array<std::optional<NodeId>, 2> heads_;
    std::array<std::optional<NodeId>, 2> tails_;
};

}  // namespace dwpdsim
