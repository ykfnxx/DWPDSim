#include "dwpdsim/policies/wear_share_round_robin_storage_policy.hpp"

#include <algorithm>
#include <cassert>
#include <chrono>
#include <stdexcept>

namespace dwpdsim {
namespace {

class PolicyTimer {
  public:
    PolicyTimer(bool enabled, std::uint64_t& elapsed) : enabled_(enabled), elapsed_(elapsed) {
        if (enabled_) { start_ = std::chrono::steady_clock::now(); }
    }
    ~PolicyTimer() {
        if (enabled_) {
            elapsed_ += std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - start_).count();
        }
    }
  private:
    bool enabled_;
    std::uint64_t& elapsed_;
    std::chrono::steady_clock::time_point start_;
};

}  // namespace

WearShareRoundRobinStoragePolicy::WearShareRoundRobinStoragePolicy(
    WearShareRoundRobinPolicyConfig config
)
    : config_(config), indexed_(config.victim_search == "indexed") {
    if (config.victim_search != "scan" && config.victim_search != "fused" && !indexed_) {
        throw std::invalid_argument("RR victim_search must be scan, fused or indexed");
    }
}

BackgroundSchedule WearShareRoundRobinStoragePolicy::background_schedule() const {
    return {};
}

void WearShareRoundRobinStoragePolicy::on_request_begin(
    const RequestContext&,
    const StorageView&
) {}

DumpPlacementDecision WearShareRoundRobinStoragePolicy::place_dump(
    const DumpContext& dump,
    const StorageView& storage
) const {
    const StorageTier tier = choose_tier(dump);
    return DumpPlacementDecision{Placement{tier, next_stream(tier, storage)}};
}

std::uint64_t WearShareRoundRobinStoragePolicy::capacity_limit_blocks(
    StorageTier tier,
    const StorageView& storage
) const {
    return static_cast<std::uint64_t>(
        static_cast<double>(storage.storage().tier(tier).capacity_blocks()) *
        config_.logical_fill_fraction
    );
}

std::optional<CapacityAction> WearShareRoundRobinStoragePolicy::reclaim_for(
    const CapacityPressureContext& pressure,
    const StorageView& storage
) const {
    PolicyTimer timer(config_.profile, work_.decision_ns);
    ++work_.decisions;
    std::optional<StoragePolicyState::SegmentTime> victim;
    if (indexed_) {
        victim = indexed_victim(pressure, storage);
    } else if (config_.victim_search == "fused") {
        victim = state_.lru_leaf_fused(
            pressure.target_tier, pressure.protected_nodes, storage, work_);
    } else {
        victim = state_.lru_leaf(
            pressure.target_tier, pressure.protected_nodes, storage, &work_);
    }
    if (config_.verify_victims) {
        const auto expected = state_.lru_leaf(
            pressure.target_tier, pressure.protected_nodes, storage);
        if (victim.has_value() != expected.has_value() ||
            (victim && (victim->endpoint != expected->endpoint ||
                        victim->timestamp_ns != expected->timestamp_ns))) {
            const std::string actual_text = victim
                ? std::to_string(victim->endpoint) + ":" + std::to_string(victim->timestamp_ns)
                : "none";
            const std::string expected_text = expected
                ? std::to_string(expected->endpoint) + ":" + std::to_string(expected->timestamp_ns)
                : "none";
            throw std::logic_error("RR victim differs from original scan: actual=" + actual_text +
                                   " expected=" + expected_text);
        }
        ++work_.verified_decisions;
    }
    if (!victim.has_value()) {
        return std::nullopt;
    }
    return TrimIntent{victim->endpoint, pressure.target_tier};
}

std::optional<MaintenanceAction>
WearShareRoundRobinStoragePolicy::next_background_action(
    const BackgroundTickContext&,
    const StorageView&
) const {
    return std::nullopt;
}

std::optional<MaintenanceAction>
WearShareRoundRobinStoragePolicy::on_storage_access(
    const StorageAccessContext&,
    const StorageView&
) const {
    return std::nullopt;
}

void WearShareRoundRobinStoragePolicy::on_commit(
    const StorageMutation& mutation,
    const StorageView& storage_after_commit
) {
    PolicyTimer timer(config_.profile, work_.maintenance_ns);
    if (!indexed_ || config_.verify_victims) {
        state_.on_commit(mutation, storage_after_commit);
    }
    const bool inserted = mutation.kind == StorageMutationKind::DumpWriteCommitted;
    const bool trimmed = mutation.kind == StorageMutationKind::CapacityTrimCommitted;
    if (inserted) {
        const auto tier = storage_tier_index(mutation.placement.tier);
        program_bytes_[tier] += mutation.bytes;
        ++write_segments_[tier];
    }
    if (!indexed_) { return; }
    const RadixTree& tree = storage_after_commit.tree();
    if (config_.subtree_counts && (inserted || trimmed)) {
        for (NodeId id : mutation.nodes) { change_ancestors(id, inserted, tree); }
    }
    if (inserted || trimmed || mutation.kind == StorageMutationKind::StorageAccessCommitted) {
        // Capacity trims may merge the dump's original segment with a surviving suffix.
        refresh_segment(tree.segment_leaf_for(mutation.segment_endpoint), tree);
    }
}

StoragePolicyStats WearShareRoundRobinStoragePolicy::stats(const StorageView&) const {
    StoragePolicyStats result;
    result.slc_program_bytes = program_bytes_[0];
    result.tlc_program_bytes = program_bytes_[1];
    return result;
}

StorageTier WearShareRoundRobinStoragePolicy::choose_tier(const DumpContext& dump) const {
    const double slc = static_cast<double>(program_bytes_[0]);
    const double tlc = static_cast<double>(program_bytes_[1]);
    const double slc_score = (slc + static_cast<double>(dump.write_bytes)) /
                             config_.slc_host_share;
    const double tlc_score = (tlc + static_cast<double>(dump.write_bytes)) /
                             (1.0 - config_.slc_host_share);
    return slc_score <= tlc_score ? StorageTier::Slc : StorageTier::Tlc;
}

std::uint32_t WearShareRoundRobinStoragePolicy::next_stream(
    StorageTier tier,
    const StorageView& storage
) const {
    const std::uint32_t count = storage.storage().tier(tier).stream_count();
    return static_cast<std::uint32_t>(write_segments_[storage_tier_index(tier)] % count);
}

void WearShareRoundRobinStoragePolicy::erase_segment(NodeId endpoint) {
    const auto found = segments_.find(endpoint);
    if (found == segments_.end()) { return; }
    for (std::size_t tier = 0; tier < 2; ++tier) {
        if (found->second.resident_blocks[tier]) {
            candidates_[tier].erase({found->second.last_ns[tier], endpoint});
        }
    }
    segments_.erase(found);
}

void WearShareRoundRobinStoragePolicy::refresh_segment(NodeId endpoint, const RadixTree& tree) {
    erase_segment(endpoint);
    Segment segment;
    tree.resolve_segment(endpoint, scratch_);
    for (NodeId id : scratch_) {
        ++work_.segment_nodes_examined;
        const Node& node = tree.node(id);
        if (!node.on_storage) { continue; }
        const auto tier = storage_tier_index(node.storage_tier);
        ++segment.resident_blocks[tier];
        segment.last_ns[tier] = std::max(segment.last_ns[tier], node.storage_last_access_timestamp_ns);
    }
    if (segment.resident_blocks[0] == 0 && segment.resident_blocks[1] == 0) { return; }
    segments_.emplace(endpoint, segment);
    for (std::size_t tier = 0; tier < 2; ++tier) {
        if (segment.resident_blocks[tier]) {
            candidates_[tier].emplace(segment.last_ns[tier], endpoint);
        }
    }
}

void WearShareRoundRobinStoragePolicy::on_node_created(NodeId node_id, const StorageView& storage) {
    if (!indexed_) { return; }
    PolicyTimer timer(config_.profile, work_.maintenance_ns);
    const RadixTree& tree = storage.tree();
    const auto parent = tree.parent(node_id);
    if (!parent) { return; }
    if (tree.child_count(*parent) == 1) {
        if (!segments_.count(*parent)) { return; }
        erase_segment(*parent);
        refresh_segment(node_id, tree);
    } else if (tree.child_count(*parent) == 2) {
        // The old suffix keeps its endpoint; the prefix becomes a separate segment.
        std::vector<NodeId> children;
        tree.children(*parent, children);
        const NodeId sibling = children[0] == node_id ? children[1] : children[0];
        const NodeId suffix = tree.segment_leaf_for(sibling);
        if (!segments_.count(suffix)) { return; }
        refresh_segment(*parent, tree);
        refresh_segment(suffix, tree);
    }
}

void WearShareRoundRobinStoragePolicy::on_node_pruned(
    NodeId node_id, std::optional<NodeId> parent, const StorageView& storage
) {
    if (!indexed_) { return; }
    PolicyTimer timer(config_.profile, work_.maintenance_ns);
    const bool had_segment = segments_.count(node_id) != 0;
    erase_segment(node_id);
    if (!parent) { return; }
    const RadixTree& tree = storage.tree();
    if (tree.child_count(*parent) == 0) {
        if (had_segment) { refresh_segment(*parent, tree); }
    } else if (tree.child_count(*parent) == 1) {
        const NodeId suffix = tree.segment_leaf_for(*parent);
        if (segments_.count(*parent) || segments_.count(suffix)) {
            erase_segment(*parent);
            refresh_segment(suffix, tree);
        }
    }
}

void WearShareRoundRobinStoragePolicy::change_ancestors(
    NodeId node_id, bool insert, const RadixTree& tree
) {
    std::optional<NodeId> current = node_id;
    while (current) {
        ++work_.ancestor_updates;
        if (insert) { ++subtree_storage_[*current]; }
        else {
            auto found = subtree_storage_.find(*current);
            assert(found != subtree_storage_.end() && found->second > 0);
            if (--found->second == 0) { subtree_storage_.erase(found); }
        }
        current = tree.parent(*current);
    }
}

std::optional<StoragePolicyState::SegmentTime> WearShareRoundRobinStoragePolicy::indexed_victim(
    const CapacityPressureContext& pressure, const StorageView& storage
) const {
    for (const auto& [last_ns, endpoint] : candidates_[storage_tier_index(pressure.target_tier)]) {
        ++work_.candidates_examined;
        if (config_.subtree_counts) {
            const auto subtree = subtree_storage_.find(endpoint);
            const std::uint64_t count = subtree == subtree_storage_.end() ? 0 : subtree->second;
            if (count != static_cast<std::uint64_t>(storage.tree().node(endpoint).on_storage)) {
                continue;
            }
        } else if (storage.tree().has_storage_descendant(endpoint)) { continue; }
        if (!storage.intersects_protected(endpoint, pressure.protected_nodes)) {
            return StoragePolicyState::SegmentTime{endpoint, last_ns};
        }
    }
    return std::nullopt;
}

StoragePolicyWork WearShareRoundRobinStoragePolicy::work() const {
    auto result = work_;
    result.indexed_segments = segments_.size();
    result.ancestor_entries = subtree_storage_.size();
    return result;
}

}  // namespace dwpdsim
