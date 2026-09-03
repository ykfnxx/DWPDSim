#pragma once

#include <cstddef>
#include <optional>
#include <unordered_map>
#include <utility>
#include <vector>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

class RadixTree {
  public:
    RadixTree();

    std::pair<NodeId, bool> get_or_create_root(HashId hash_id, TimestampNs timestamp_ns);
    std::pair<NodeId, bool> get_or_create(
        NodeId parent_id,
        HashId hash_id,
        TimestampNs timestamp_ns
    );

    std::optional<NodeId> find(HashId hash_id) const noexcept;
    std::optional<NodeId> find_root_child(HashId hash_id) const noexcept;
    std::optional<NodeId> find_child(NodeId parent_id, HashId hash_id) const;

    Node& node(NodeId node_id) noexcept;
    const Node& node(NodeId node_id) const noexcept;

    bool contains(NodeId node_id) const noexcept;
    std::optional<NodeId> parent(NodeId node_id) const noexcept;
    std::uint32_t child_count(NodeId node_id) const noexcept;
    bool is_leaf(NodeId node_id) const noexcept;
    NodeId segment_top(NodeId endpoint) const noexcept;
    void resolve_segment(NodeId endpoint, std::vector<NodeId>& segment) const;
    NodeId segment_leaf_for(NodeId node_id) const noexcept;
    void children(NodeId node_id, std::vector<NodeId>& output) const;
    bool has_storage_descendant(NodeId node_id) const;

    void record_access(NodeId node_id, TimestampNs timestamp_ns, bool hit) noexcept;

    std::optional<NodeId> detach_leaf(NodeId node_id);
    void release_detached(NodeId node_id) noexcept;

    std::size_t size() const noexcept;

  private:
    struct NodeRecord {
        Node node;
        NodeSlot parent = kInvalidNodeSlot;
        NodeSlot first_child = kInvalidNodeSlot;
        NodeSlot previous_sibling = kInvalidNodeSlot;
        NodeSlot next_sibling = kInvalidNodeSlot;
        std::uint32_t child_count = 0;
    };

    std::pair<NodeId, bool> get_or_create_at(
        NodeSlot parent_slot,
        HashId hash_id,
        TimestampNs timestamp_ns
    );
    NodeSlot slot(NodeId node_id) const noexcept;
    NodeId id(NodeSlot slot) const noexcept;

    static constexpr NodeSlot kRootSlot = 0;

    std::vector<NodeRecord> nodes_;
    std::unordered_map<NodeId, NodeSlot> node_index_;
    std::unordered_map<NodeId, NodeSlot> detached_index_;
    std::vector<NodeSlot> free_slots_;
    std::size_t active_nodes_ = 0;
};

}  // namespace dwpdsim
