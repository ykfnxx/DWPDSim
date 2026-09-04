#include "dwpdsim/policies/baseline_memory_lru_policy.hpp"

#include "dwpdsim/radix_tree.hpp"

#include <unordered_set>

namespace dwpdsim {

BaselineMemoryLruPolicy::BaselineMemoryLruPolicy(
    bool admit_storage_hits,
    MemoryEvictionAction eviction_action
)
    : admit_storage_hits_(admit_storage_hits), eviction_action_(eviction_action) {}

bool BaselineMemoryLruPolicy::admit_storage_hit(
    const AccessContext&,
    const Node&,
    const RadixTree&
) const {
    return admit_storage_hits_;
}

MemoryEvictionDecision BaselineMemoryLruPolicy::evict(
    const RequestContext&,
    const RadixTree& tree
) const {
    std::unordered_set<NodeId> seen_segments;
    std::optional<NodeId> victim;
    std::optional<NodeId> current = head_;
    while (current.has_value()) {
        const NodeId endpoint = tree.segment_leaf_for(*current);
        if (
            seen_segments.insert(endpoint).second &&
            !tree.has_memory_descendant(endpoint)
        ) {
            victim = endpoint;
        }
        current = links_.at(*current).next;
    }
    return MemoryEvictionDecision{
        *victim,
        eviction_action_,
    };
}

void BaselineMemoryLruPolicy::on_commit(const MemoryMutation& mutation) {
    switch (mutation.kind) {
        case MemoryMutationKind::Inserted:
            attach_front(mutation.node_id);
            break;
        case MemoryMutationKind::Accessed:
            detach(mutation.node_id);
            attach_front(mutation.node_id);
            break;
        case MemoryMutationKind::Removed:
            detach(mutation.node_id);
            break;
    }
}

void BaselineMemoryLruPolicy::attach_front(NodeId node_id) {
    Link& link = links_[node_id];
    link.previous.reset();
    link.next = head_;
    if (head_.has_value()) {
        links_.at(*head_).previous = node_id;
    } else {
        tail_ = node_id;
    }
    head_ = node_id;
}

void BaselineMemoryLruPolicy::detach(NodeId node_id) {
    const auto entry = links_.find(node_id);
    const Link link = entry->second;
    if (link.previous.has_value()) {
        links_.at(*link.previous).next = link.next;
    } else {
        head_ = link.next;
    }
    if (link.next.has_value()) {
        links_.at(*link.next).previous = link.previous;
    } else {
        tail_ = link.previous;
    }
    links_.erase(entry);
}

}  // namespace dwpdsim
