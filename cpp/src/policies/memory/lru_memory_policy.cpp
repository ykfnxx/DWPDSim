#include "dwpdsim/policies/memory/lru_memory_policy.hpp"

#include "dwpdsim/radix_tree.hpp"

namespace dwpdsim {

LruMemoryPolicy::LruMemoryPolicy(bool admit_storage_hits, EvictionAction eviction_action)
    : admit_storage_hits_(admit_storage_hits), eviction_action_(eviction_action) {}

bool LruMemoryPolicy::admit_storage_hit(
    const AccessContext& context,
    const Node& node,
    const RadixTree& tree
) {
    static_cast<void>(context);
    static_cast<void>(node);
    static_cast<void>(tree);
    return admit_storage_hits_;
}

NodeId LruMemoryPolicy::choose_victim(
    const AccessContext& context,
    const RadixTree& tree
) {
    static_cast<void>(context);
    return tree.segment_leaf_for(*tail_);
}

EvictionAction LruMemoryPolicy::eviction_action(
    const Node& victim,
    const AccessContext& context,
    const RadixTree& tree
) {
    static_cast<void>(victim);
    static_cast<void>(context);
    static_cast<void>(tree);
    return eviction_action_;
}

void LruMemoryPolicy::on_memory_insert(NodeId node_id) {
    attach_front(node_id);
}

void LruMemoryPolicy::on_memory_access(NodeId node_id) {
    detach(node_id);
    attach_front(node_id);
}

void LruMemoryPolicy::on_memory_remove(NodeId node_id) {
    detach(node_id);
}

void LruMemoryPolicy::attach_front(NodeId node_id) noexcept {
    Link& link = links_[node_id];
    link.previous.reset();
    link.next = head_;
    if (head_.has_value()) {
        links_.find(*head_)->second.previous = node_id;
    } else {
        tail_ = node_id;
    }
    head_ = node_id;
}

void LruMemoryPolicy::detach(NodeId node_id) noexcept {
    const auto entry = links_.find(node_id);
    const Link link = entry->second;
    if (link.previous.has_value()) {
        links_.find(*link.previous)->second.next = link.next;
    } else {
        head_ = link.next;
    }
    if (link.next.has_value()) {
        links_.find(*link.next)->second.previous = link.previous;
    } else {
        tail_ = link.previous;
    }
    links_.erase(entry);
}

}  // namespace dwpdsim
