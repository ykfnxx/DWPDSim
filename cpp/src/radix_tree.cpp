#include "dwpdsim/radix_tree.hpp"

#include <cstddef>
#include <cstdint>

namespace dwpdsim {
namespace {

std::uint64_t mix(std::uint64_t value) noexcept {
    value += 0x9e3779b97f4a7c15ULL;
    value = (value ^ (value >> 30U)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27U)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31U);
}

}  // namespace

std::size_t EdgeKeyHash::operator()(const EdgeKey& key) const noexcept {
    const std::uint64_t parent = mix(key.parent_id);
    const std::uint64_t combined =
        parent ^ (mix(key.hash_id) + 0x9e3779b97f4a7c15ULL + (parent << 6U) + (parent >> 2U));
    return static_cast<std::size_t>(combined);
}

RadixTree::RadixTree() {
    nodes_.push_back(Node{});
}

std::pair<NodeId, bool> RadixTree::get_or_create(
    NodeId parent_id,
    HashId hash_id,
    Timestamp timestamp
) {
    const EdgeKey key{parent_id, hash_id};
    const auto existing = child_index_.find(key);
    if (existing != child_index_.end()) {
        return {existing->second, false};
    }

    const NodeId node_id = static_cast<NodeId>(nodes_.size());
    Node node;
    node.parent_id = parent_id;
    node.hash_id = hash_id;
    node.first_seen_timestamp = timestamp;
    node.last_access_timestamp = timestamp;
    nodes_.push_back(node);
    child_index_.emplace(key, node_id);
    return {node_id, true};
}

std::optional<NodeId> RadixTree::find_child(NodeId parent_id, HashId hash_id) const {
    const auto existing = child_index_.find(EdgeKey{parent_id, hash_id});
    if (existing == child_index_.end()) {
        return std::nullopt;
    }
    return existing->second;
}

Node& RadixTree::node(NodeId node_id) noexcept {
    return nodes_[static_cast<std::size_t>(node_id)];
}

const Node& RadixTree::node(NodeId node_id) const noexcept {
    return nodes_[static_cast<std::size_t>(node_id)];
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

std::size_t RadixTree::size() const noexcept {
    return nodes_.size();
}

}  // namespace dwpdsim
