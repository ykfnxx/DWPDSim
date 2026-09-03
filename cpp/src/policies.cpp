#include "dwpdsim/policies.hpp"

#include <cstddef>

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

FixedPlacementPolicy::FixedPlacementPolicy(Medium medium, std::uint32_t stream_id)
    : placement_{medium, stream_id} {}

Placement FixedPlacementPolicy::place(
    const Node& node,
    const AccessContext& context,
    const RadixTree& tree,
    const StorageSummary& storage
) {
    static_cast<void>(node);
    static_cast<void>(context);
    static_cast<void>(tree);
    static_cast<void>(storage);
    return placement_;
}

RatioPlacementPolicy::RatioPlacementPolicy(
    double slc_ratio,
    std::uint32_t slc_stream_count,
    std::uint32_t tlc_stream_count
)
    : slc_ratio_(slc_ratio), stream_counts_{slc_stream_count, tlc_stream_count} {}

Placement RatioPlacementPolicy::place(
    const Node& node,
    const AccessContext& context,
    const RadixTree& tree,
    const StorageSummary& storage
) {
    static_cast<void>(node);
    static_cast<void>(context);
    static_cast<void>(tree);
    static_cast<void>(storage);

    const std::uint64_t next_total = write_counts_[0] + write_counts_[1] + 1;
    const double target_slc_writes = static_cast<double>(next_total) * slc_ratio_;
    const Medium medium = static_cast<double>(write_counts_[0]) < target_slc_writes
                              ? Medium::Slc
                              : Medium::Tlc;
    const std::size_t index = medium_index(medium);
    const std::uint32_t stream_id = next_stream_[index] % stream_counts_[index];
    ++next_stream_[index];
    ++write_counts_[index];
    return Placement{medium, stream_id};
}

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
