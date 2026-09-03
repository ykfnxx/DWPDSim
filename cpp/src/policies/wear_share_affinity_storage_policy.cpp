#include "dwpdsim/policies/wear_share_affinity_storage_policy.hpp"

namespace dwpdsim {

WearShareAffinityStoragePolicy::WearShareAffinityStoragePolicy(
    WearShareAffinityPolicyConfig config
)
    : config_(config) {}

BackgroundSchedule WearShareAffinityStoragePolicy::background_schedule() const {
    return {};
}

void WearShareAffinityStoragePolicy::on_request_begin(
    const RequestContext&,
    const StorageView&
) {}

DumpPlacementDecision WearShareAffinityStoragePolicy::place_dump(
    const DumpContext& dump,
    const StorageView& storage
) const {
    const StorageTier tier = choose_tier(dump);
    const AffinityId affinity = dump.request.affinity_id == 0
                                    ? dump.segment.segment_endpoint
                                    : dump.request.affinity_id;
    return DumpPlacementDecision{Placement{
        tier,
        stream_for(tier, affinity, dump.segment.segment_endpoint, storage),
    }};
}

std::uint64_t WearShareAffinityStoragePolicy::capacity_limit_blocks(
    StorageTier tier,
    const StorageView& storage
) const {
    return static_cast<std::uint64_t>(
        static_cast<double>(storage.storage().tier(tier).capacity_blocks()) *
        config_.logical_fill_fraction
    );
}

std::optional<CapacityAction> WearShareAffinityStoragePolicy::reclaim_for(
    const CapacityPressureContext& pressure,
    const StorageView& storage
) const {
    const auto victim = state_.lru_leaf(
        pressure.target_tier,
        pressure.protected_nodes,
        storage
    );
    if (!victim.has_value()) {
        return std::nullopt;
    }
    return TrimIntent{victim->endpoint, pressure.target_tier};
}

std::optional<MaintenanceAction>
WearShareAffinityStoragePolicy::next_background_action(
    const BackgroundTickContext&,
    const StorageView&
) const {
    return std::nullopt;
}

std::optional<MaintenanceAction> WearShareAffinityStoragePolicy::on_storage_access(
    const StorageAccessContext&,
    const StorageView&
) const {
    return std::nullopt;
}

void WearShareAffinityStoragePolicy::on_commit(
    const StorageMutation& mutation,
    const StorageView& storage_after_commit
) {
    state_.on_commit(mutation, storage_after_commit);
}

StoragePolicyStats WearShareAffinityStoragePolicy::stats(const StorageView&) const {
    StoragePolicyStats result;
    result.slc_program_bytes = state_.program_bytes(StorageTier::Slc);
    result.tlc_program_bytes = state_.program_bytes(StorageTier::Tlc);
    return result;
}

std::uint64_t WearShareAffinityStoragePolicy::stable_hash(std::uint64_t value) {
    std::uint64_t result = value + 0x9E3779B97F4A7C15ULL + 11;
    result = (result ^ (result >> 30)) * 0xBF58476D1CE4E5B9ULL;
    result = (result ^ (result >> 27)) * 0x94D049BB133111EBULL;
    return result ^ (result >> 31);
}

StorageTier WearShareAffinityStoragePolicy::choose_tier(const DumpContext& dump) const {
    const double slc = static_cast<double>(state_.program_bytes(StorageTier::Slc));
    const double tlc = static_cast<double>(state_.program_bytes(StorageTier::Tlc));
    const double slc_score = (slc + static_cast<double>(dump.write_bytes)) /
                             config_.slc_host_share;
    const double tlc_score = (tlc + static_cast<double>(dump.write_bytes)) /
                             (1.0 - config_.slc_host_share);
    return slc_score <= tlc_score ? StorageTier::Slc : StorageTier::Tlc;
}

std::uint32_t WearShareAffinityStoragePolicy::stream_for(
    StorageTier tier,
    AffinityId affinity_id,
    NodeId segment_endpoint,
    const StorageView& storage
) const {
    const std::uint64_t key = affinity_id == 0 ? segment_endpoint : affinity_id;
    return static_cast<std::uint32_t>(
        stable_hash(key) % storage.storage().tier(tier).stream_count()
    );
}

}  // namespace dwpdsim
