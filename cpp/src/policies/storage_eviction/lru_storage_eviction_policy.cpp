#include "dwpdsim/policies/storage_eviction/lru_storage_eviction_policy.hpp"

#include <cstddef>

#include "dwpdsim/radix_tree.hpp"

namespace dwpdsim {

NodeId LruStorageEvictionPolicy::choose_victim(
    Medium medium,
    NodeId incoming_node,
    const AccessContext& context,
    const RadixTree& tree
) {
    static_cast<void>(incoming_node);
    static_cast<void>(context);
    return tree.segment_leaf_for(*tails_[medium_index(medium)]);
}

void LruStorageEvictionPolicy::on_storage_read(NodeId node_id, Medium medium) {
    detach(node_id);
    attach_front(node_id, medium);
}

void LruStorageEvictionPolicy::on_storage_write(NodeId node_id, Medium medium) {
    attach_front(node_id, medium);
}

void LruStorageEvictionPolicy::on_storage_remove(NodeId node_id, Medium medium) {
    static_cast<void>(medium);
    detach(node_id);
}

void LruStorageEvictionPolicy::attach_front(NodeId node_id, Medium medium) noexcept {
    const std::size_t index = medium_index(medium);
    Link& link = links_[node_id];
    link.previous.reset();
    link.next = heads_[index];
    link.medium = medium;
    if (heads_[index].has_value()) {
        links_.find(*heads_[index])->second.previous = node_id;
    } else {
        tails_[index] = node_id;
    }
    heads_[index] = node_id;
}

void LruStorageEvictionPolicy::detach(NodeId node_id) noexcept {
    const auto entry = links_.find(node_id);
    const Link link = entry->second;
    const std::size_t index = medium_index(link.medium);
    if (link.previous.has_value()) {
        links_.find(*link.previous)->second.next = link.next;
    } else {
        heads_[index] = link.next;
    }
    if (link.next.has_value()) {
        links_.find(*link.next)->second.previous = link.previous;
    } else {
        tails_[index] = link.previous;
    }
    links_.erase(entry);
}

}  // namespace dwpdsim
