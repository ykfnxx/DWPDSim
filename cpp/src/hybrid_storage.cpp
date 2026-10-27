#include "dwpdsim/simulator.hpp"

#include <algorithm>
#include <set>

namespace dwpdsim {

bool Simulator::hybrid_ensure_capacity(StorageTier tier, std::uint64_t units,
    const std::vector<NodeId>& protected_nodes, TimestampNs now) {
    const auto limit = storage_policy_->capacity_limit_blocks(tier, storage_view());
    if (units > limit) { return false; }
    while (storage_.tier(tier).used_blocks() + units > limit) {
        const auto action = storage_policy_->reclaim_for(CapacityPressureContext{
            now, CapacityCause::DumpAdmission, tier, units,
            NodeSpan{protected_nodes.data(), protected_nodes.size()}}, storage_view());
        if (!action) { return false; }
        if (!execute_action(*action, ActionPlane::Capacity, protected_nodes,
                TraceContext{now, hybrid_->current_request_id, std::nullopt})) { return false; }
    }
    return true;
}

bool Simulator::hybrid_execute_trim(const TrimIntent& intent, ActionPlane plane, TimestampNs now) {
    const auto nodes = storage_view().resident_nodes(intent.segment_endpoint, intent.tier);
    if (nodes.empty()) { return false; }
    std::uint64_t bytes = 0;
    const auto reason = plane == ActionPlane::Capacity ?
        TraceReason::CapacityEviction : TraceReason::IdleEviction;
    auto& tier_counts = hybrid_->storage_counters[storage_tier_index(intent.tier)];
    for (const auto id : nodes) {
        auto& node = tree_.node(id);
        for (std::size_t kind = 0; kind < 2; ++kind) {
            auto& c = hybrid_data(id, kind);
            if (!c.on_storage()) { continue; }
            hybrid_io(id, kind, "TRIM", now, reason);
            bytes += c.storage_units * config_.block_size_bytes;
            for (const auto address : c.storage_addresses) { storage_.tier(intent.tier).release(address); }
            auto& stats = hybrid_->counters[kind];
            stats.storage_bytes -= c.size_bytes;
            stats.storage_allocated_bytes -= c.storage_units * config_.block_size_bytes;
            ++stats.trim_objects;
            stats.trim_bytes += c.size_bytes;
            c.storage_addresses.clear();
            c.storage_units = 0;
        }
        node.clear_storage_location();
    }
    if (plane == ActionPlane::Capacity) { ++tier_counts.capacity_trims; }
    else { ++tier_counts.idle_trims; }
    notify_storage_commit(StorageMutation{
        plane == ActionPlane::Capacity ? StorageMutationKind::CapacityTrimCommitted :
            StorageMutationKind::IdleTrimCommitted,
        now, 0, intent.segment_endpoint, NodeSpan{nodes.data(), nodes.size()},
        Placement{intent.tier, 0}, intent.tier, bytes});
    // Hybrid metadata retains logical identity and LRU-K history after losing a copy.
    return true;
}

bool Simulator::hybrid_execute_relocation(const RelocateIntent& intent,
    const std::vector<NodeId>& protected_nodes, TimestampNs now) {
    const auto segment = storage_view().resolve_segment(intent.source_segment_endpoint);
    const auto source = intent.destination.tier == StorageTier::Tlc ? StorageTier::Slc : StorageTier::Tlc;
    const auto nodes = storage_view().resident_nodes(intent.source_segment_endpoint, source);
    if (nodes.empty()) { return false; }
    std::uint64_t units = 0;
    for (const auto id : nodes) {
        for (const auto& c : tree_.node(id).hybrid->components) { units += c.storage_units; }
    }
    auto protection = protected_nodes;
    protection.insert(protection.end(), segment.ordered_nodes.begin(), segment.ordered_nodes.end());
    if (!hybrid_ensure_capacity(intent.destination.tier, units, protection, now)) { return false; }
    const auto move = next_move_id_++;
    const auto reason = intent.cause == RelocationCause::Access ?
        TraceReason::AccessMigration : TraceReason::BackgroundMigration;
    for (const auto id : nodes) {
        auto& node = tree_.node(id);
        const auto old_location = node.storage_location();
        for (std::size_t kind = 0; kind < 2; ++kind) {
            auto& c = hybrid_data(id, kind);
            if (!c.on_storage()) { continue; }
            const auto consumed = hybrid_->read_sequences[kind].find(id);
            std::uint64_t read;
            if (intent.cause == RelocationCause::Access && consumed != hybrid_->read_sequences[kind].end()) {
                read = consumed->second;
            } else {
                read = hybrid_io(id, kind, "READ", now, reason, move, std::nullopt, old_location);
                auto& stats = hybrid_->counters[kind];
                ++stats.read_objects;
                stats.read_bytes += c.size_bytes;
                stats.read_allocated_bytes += c.storage_units * config_.block_size_bytes;
            }
            auto old_addresses = std::move(c.storage_addresses);
            c.storage_addresses.clear();
            c.storage_addresses.reserve(c.storage_units);
            for (std::uint64_t i = 0; i < c.storage_units; ++i) {
                c.storage_addresses.push_back(storage_.tier(intent.destination.tier).allocate());
            }
            node.set_storage_location(StorageLocation{intent.destination.tier, 0, intent.destination.stream_id});
            const auto write = hybrid_io(id, kind, "WRITE", now, reason, move, read);
            auto& stats = hybrid_->counters[kind];
            ++stats.write_objects;
            stats.write_bytes += c.size_bytes;
            stats.write_allocated_bytes += c.storage_units * config_.block_size_bytes;
            c.storage_addresses.swap(old_addresses);
            hybrid_io(id, kind, "TRIM", now, reason, move, write, old_location);
            for (const auto address : c.storage_addresses) { storage_.tier(source).release(address); }
            c.storage_addresses.swap(old_addresses);
            ++hybrid_->storage_counters[storage_tier_index(intent.destination.tier)].migrated_objects;
        }
    }
    notify_storage_commit(StorageMutation{StorageMutationKind::RelocationWriteCommitted,
        now, hybrid_->current_affinity_id, intent.source_segment_endpoint,
        NodeSpan{nodes.data(), nodes.size()}, intent.destination, source,
        units * config_.block_size_bytes});
    return true;
}

void Simulator::hybrid_storage_accesses(
    const std::vector<std::pair<std::size_t, NodeId>>& reads, TimestampNs now) {
    if (config_.infinite_storage) { return; }
    // All sources were consumed before touching recency or migrating bundles.
    // Touch each same-tier segment once per request, not once per FULL block.
    std::set<std::pair<NodeId, StorageTier>> segments;
    for (const auto& read : reads) {
        const auto& node = tree_.node(read.second);
        segments.emplace(tree_.segment_leaf_for(read.second), node.storage_tier);
    }
    for (const auto& [endpoint, tier] : segments) {
        const auto nodes = storage_view().resident_nodes(endpoint, tier);
        notify_storage_commit(StorageMutation{StorageMutationKind::StorageAccessCommitted,
            now, hybrid_->current_affinity_id, endpoint, NodeSpan{nodes.data(), nodes.size()},
            Placement{tier, 0}, tier, 0});
    }
    for (const auto& [endpoint, tier] : segments) {
        const auto nodes = storage_view().resident_nodes(endpoint, tier);
        if (nodes.empty()) { continue; } // A prior migration can reclaim another consumed segment.
        const auto id = nodes.front();
        const RequestContext request{now, hybrid_->current_request_id, hybrid_->current_affinity_id, {}, {}};
        const auto action = storage_policy_->on_storage_access(StorageAccessContext{
            AccessContext{request, 0, 0, id, tree_.parent(id)}, tree_.node(id).storage_location()}, storage_view());
        if (action) { execute_action(*action, ActionPlane::Background, {}, TraceContext{now, request.request_id, {}}); }
    }
}

} // namespace dwpdsim
