#include "dwpdsim/policies/wear_share_round_robin_storage_policy.hpp"

namespace dwpdsim {

WearShareRoundRobinStoragePolicy::WearShareRoundRobinStoragePolicy(
    WearShareRoundRobinPolicyConfig config
)
    : config_(config) {}

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
    state_.on_commit(mutation, storage_after_commit);
}

StoragePolicyStats WearShareRoundRobinStoragePolicy::stats(const StorageView&) const {
    StoragePolicyStats result;
    result.slc_program_bytes = state_.program_bytes(StorageTier::Slc);
    result.tlc_program_bytes = state_.program_bytes(StorageTier::Tlc);
    return result;
}

StorageTier WearShareRoundRobinStoragePolicy::choose_tier(const DumpContext& dump) const {
    const double slc = static_cast<double>(state_.program_bytes(StorageTier::Slc));
    const double tlc = static_cast<double>(state_.program_bytes(StorageTier::Tlc));
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
    return static_cast<std::uint32_t>(state_.committed_write_segments(tier) % count);
}

}  // namespace dwpdsim
