#include "dwpdsim/policies/storage_policy_state.hpp"

#include <algorithm>
#include <limits>
#include <set>

namespace dwpdsim {

void StoragePolicyState::on_commit(
    const StorageMutation& mutation,
    const StorageView& storage
) {
    switch (mutation.kind) {
        case StorageMutationKind::DumpWriteCommitted:
            for (NodeId node_id : mutation.nodes) {
                entries_[node_id] = Entry{
                    mutation.placement.tier,
                    mutation.timestamp_ns,
                    mutation.timestamp_ns,
                    mutation.affinity_id,
                };
            }
            program_bytes_[storage_tier_index(mutation.placement.tier)] += mutation.bytes;
            ++committed_write_segments_[storage_tier_index(mutation.placement.tier)];
            break;
        case StorageMutationKind::CapacityTrimCommitted:
        case StorageMutationKind::IdleTrimCommitted:
            for (NodeId node_id : mutation.nodes) {
                entries_.erase(node_id);
            }
            break;
        case StorageMutationKind::RelocationWriteCommitted:
            for (NodeId node_id : mutation.nodes) {
                Entry& entry = entries_.at(node_id);
                entry.tier = mutation.placement.tier;
            }
            program_bytes_[storage_tier_index(mutation.placement.tier)] += mutation.bytes;
            ++committed_write_segments_[storage_tier_index(mutation.placement.tier)];
            break;
        case StorageMutationKind::StorageAccessCommitted:
            for (NodeId node_id : mutation.nodes) {
                auto entry = entries_.find(node_id);
                if (entry != entries_.end()) {
                    entry->second.last_ns = mutation.timestamp_ns;
                }
            }
            break;
        case StorageMutationKind::NodePruned:
            for (NodeId node_id : mutation.nodes) {
                entries_.erase(node_id);
            }
            break;
        case StorageMutationKind::RelocationReadCommitted:
        case StorageMutationKind::RelocationSourceTrimCommitted:
            break;
    }
    static_cast<void>(storage);
}

std::optional<StoragePolicyState::SegmentTime> StoragePolicyState::lru_leaf(
    StorageTier tier,
    NodeSpan protected_nodes,
    const StorageView& storage
) const {
    std::optional<SegmentTime> best;
    std::set<NodeId> visited;
    for (const auto& [node_id, entry] : entries_) {
        if (entry.tier != tier || !storage.tree().contains(node_id)) {
            continue;
        }
        const NodeId endpoint = storage.tree().segment_leaf_for(node_id);
        if (!visited.insert(endpoint).second || !storage.is_storage_leaf(endpoint) ||
            storage.resident_blocks(endpoint, tier) == 0 ||
            storage.intersects_protected(endpoint, protected_nodes)) {
            continue;
        }
        const TimestampNs last_ns = segment_last_ns(endpoint, tier, storage);
        if (!best.has_value() || last_ns < best->timestamp_ns ||
            (last_ns == best->timestamp_ns && endpoint < best->endpoint)) {
            best = SegmentTime{endpoint, last_ns};
        }
    }
    return best;
}

std::optional<StoragePolicyState::SegmentTime> StoragePolicyState::oldest_segment(
    StorageTier tier,
    const StorageView& storage
) const {
    std::optional<SegmentTime> best;
    std::set<NodeId> visited;
    for (const auto& [node_id, entry] : entries_) {
        if (entry.tier != tier || !storage.tree().contains(node_id)) {
            continue;
        }
        const NodeId endpoint = storage.tree().segment_leaf_for(node_id);
        if (!visited.insert(endpoint).second || storage.resident_blocks(endpoint, tier) == 0) {
            continue;
        }
        const TimestampNs first_ns = segment_first_ns(endpoint, tier, storage);
        if (!best.has_value() || first_ns < best->timestamp_ns ||
            (first_ns == best->timestamp_ns && endpoint < best->endpoint)) {
            best = SegmentTime{endpoint, first_ns};
        }
    }
    return best;
}

TimestampNs StoragePolicyState::segment_first_ns(
    NodeId endpoint,
    StorageTier tier,
    const StorageView& storage
) const {
    TimestampNs first_ns = std::numeric_limits<TimestampNs>::max();
    for (NodeId node_id : storage.resident_nodes(endpoint, tier)) {
        first_ns = std::min(first_ns, entries_.at(node_id).first_ns);
    }
    return first_ns;
}

TimestampNs StoragePolicyState::segment_last_ns(
    NodeId endpoint,
    StorageTier tier,
    const StorageView& storage
) const {
    TimestampNs last_ns = 0;
    for (NodeId node_id : storage.resident_nodes(endpoint, tier)) {
        last_ns = std::max(last_ns, entries_.at(node_id).last_ns);
    }
    return last_ns;
}

std::uint64_t StoragePolicyState::program_bytes(StorageTier tier) const noexcept {
    return program_bytes_[storage_tier_index(tier)];
}

std::uint64_t StoragePolicyState::committed_write_segments(
    StorageTier tier
) const noexcept {
    return committed_write_segments_[storage_tier_index(tier)];
}

}  // namespace dwpdsim
