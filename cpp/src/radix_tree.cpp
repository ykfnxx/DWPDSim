#include "dwpdsim/radix_tree.hpp"

#include <cstddef>

namespace dwpdsim {

RadixTree::RadixTree() {
    nodes_.emplace_back();
}

std::pair<NodeId, bool> RadixTree::get_or_create_root(
    HashId hash_id,
    Timestamp timestamp
) {
    return get_or_create_at(kRootSlot, hash_id, timestamp);
}

std::pair<NodeId, bool> RadixTree::get_or_create(
    NodeId parent_id,
    HashId hash_id,
    Timestamp timestamp
) {
    return get_or_create_at(slot(parent_id), hash_id, timestamp);
}

std::pair<NodeId, bool> RadixTree::get_or_create_at(
    NodeSlot parent_slot,
    HashId hash_id,
    Timestamp timestamp
) {
    const auto existing = node_index_.find(hash_id);
    if (existing != node_index_.end()) {
        return {hash_id, false};
    }

    NodeSlot node_slot;
    if (free_slots_.empty()) {
        node_slot = static_cast<NodeSlot>(nodes_.size());
        nodes_.emplace_back();
    } else {
        node_slot = free_slots_.back();
        free_slots_.pop_back();
    }

    NodeRecord& record = nodes_[node_slot];
    record = NodeRecord{};
    record.node.hash_id = hash_id;
    record.node.first_seen_timestamp = timestamp;
    record.parent = parent_slot;

    NodeRecord& parent = nodes_[parent_slot];
    record.next_sibling = parent.first_child;
    if (parent.first_child != kInvalidNodeSlot) {
        nodes_[parent.first_child].previous_sibling = node_slot;
    }
    parent.first_child = node_slot;
    ++parent.child_count;

    node_index_.emplace(hash_id, node_slot);
    ++active_nodes_;
    return {hash_id, true};
}

std::optional<NodeId> RadixTree::find(HashId hash_id) const noexcept {
    if (node_index_.find(hash_id) == node_index_.end()) {
        return std::nullopt;
    }
    return hash_id;
}

std::optional<NodeId> RadixTree::find_root_child(HashId hash_id) const noexcept {
    const auto existing = node_index_.find(hash_id);
    if (existing == node_index_.end() || nodes_[existing->second].parent != kRootSlot) {
        return std::nullopt;
    }
    return hash_id;
}

std::optional<NodeId> RadixTree::find_child(NodeId parent_id, HashId hash_id) const {
    const auto existing = node_index_.find(hash_id);
    if (
        existing == node_index_.end() ||
        nodes_[existing->second].parent != slot(parent_id)
    ) {
        return std::nullopt;
    }
    return hash_id;
}

Node& RadixTree::node(NodeId node_id) noexcept {
    return nodes_[slot(node_id)].node;
}

const Node& RadixTree::node(NodeId node_id) const noexcept {
    return nodes_[slot(node_id)].node;
}

bool RadixTree::contains(NodeId node_id) const noexcept {
    return node_index_.find(node_id) != node_index_.end();
}

std::optional<NodeId> RadixTree::parent(NodeId node_id) const noexcept {
    const NodeSlot parent_slot = nodes_[slot(node_id)].parent;
    if (parent_slot == kRootSlot) {
        return std::nullopt;
    }
    return id(parent_slot);
}

std::uint32_t RadixTree::child_count(NodeId node_id) const noexcept {
    return nodes_[slot(node_id)].child_count;
}

bool RadixTree::is_leaf(NodeId node_id) const noexcept {
    return child_count(node_id) == 0;
}

NodeId RadixTree::segment_top(NodeId endpoint) const noexcept {
    NodeSlot current = slot(endpoint);
    while (true) {
        const NodeSlot parent_slot = nodes_[current].parent;
        if (parent_slot == kRootSlot || nodes_[parent_slot].child_count >= 2) {
            return id(current);
        }
        current = parent_slot;
    }
}

void RadixTree::resolve_segment(NodeId endpoint, std::vector<NodeId>& segment) const {
    segment.clear();
    NodeSlot current = slot(endpoint);
    while (true) {
        segment.push_back(id(current));
        const NodeSlot parent_slot = nodes_[current].parent;
        if (parent_slot == kRootSlot || nodes_[parent_slot].child_count >= 2) {
            return;
        }
        current = parent_slot;
    }
}

NodeId RadixTree::segment_leaf_for(NodeId node_id) const noexcept {
    NodeSlot current = slot(node_id);
    while (nodes_[current].child_count == 1) {
        current = nodes_[current].first_child;
    }
    return id(current);
}

void RadixTree::record_access(NodeId node_id, Timestamp timestamp, bool hit) noexcept {
    Node& entry = node(node_id);
    entry.last_access_timestamp = timestamp;
    ++entry.access_count;
    if (hit) {
        entry.last_hit_timestamp = timestamp;
        entry.has_last_hit = true;
    }
}

std::optional<NodeId> RadixTree::detach_leaf(NodeId node_id) {
    const NodeSlot node_slot = slot(node_id);
    NodeRecord& record = nodes_[node_slot];
    const NodeSlot parent_slot = record.parent;
    NodeRecord& parent = nodes_[parent_slot];

    if (record.previous_sibling == kInvalidNodeSlot) {
        parent.first_child = record.next_sibling;
    } else {
        nodes_[record.previous_sibling].next_sibling = record.next_sibling;
    }
    if (record.next_sibling != kInvalidNodeSlot) {
        nodes_[record.next_sibling].previous_sibling = record.previous_sibling;
    }
    --parent.child_count;

    node_index_.erase(node_id);
    detached_index_.emplace(node_id, node_slot);
    --active_nodes_;

    if (parent_slot == kRootSlot) {
        return std::nullopt;
    }
    return id(parent_slot);
}

void RadixTree::release_detached(NodeId node_id) noexcept {
    const auto detached = detached_index_.find(node_id);
    const NodeSlot node_slot = detached->second;
    detached_index_.erase(detached);
    nodes_[node_slot] = NodeRecord{};
    free_slots_.push_back(node_slot);
}

std::size_t RadixTree::size() const noexcept {
    return active_nodes_;
}

NodeSlot RadixTree::slot(NodeId node_id) const noexcept {
    return node_index_.find(node_id)->second;
}

NodeId RadixTree::id(NodeSlot node_slot) const noexcept {
    return nodes_[node_slot].node.hash_id;
}

}  // namespace dwpdsim
