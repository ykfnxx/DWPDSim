#include "dwpdsim/policies/baseline_fixed_lru_storage_policy.hpp"

namespace dwpdsim {

BaselineFixedLruStoragePolicy::BaselineFixedLruStoragePolicy(Placement placement)
    : placement_(placement) {}

BackgroundSchedule BaselineFixedLruStoragePolicy::background_schedule() const {
    return {};
}

void BaselineFixedLruStoragePolicy::on_request_begin(
    const RequestContext&,
    const StorageView&
) {}

DumpPlacementDecision BaselineFixedLruStoragePolicy::place_dump(
    const DumpContext&,
    const StorageView&
) const {
    return DumpPlacementDecision{placement_};
}

std::uint64_t BaselineFixedLruStoragePolicy::capacity_limit_blocks(
    StorageTier tier,
    const StorageView& storage
) const {
    return storage.storage().tier(tier).capacity_blocks();
}

std::optional<CapacityAction> BaselineFixedLruStoragePolicy::reclaim_for(
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
    if (pressure.target_tier == StorageTier::Slc) {
        const std::uint32_t stream = placement_.tier == StorageTier::Tlc
                                         ? placement_.stream_id
                                         : 0;
        return RelocateIntent{
            victim->endpoint,
            Placement{StorageTier::Tlc, stream},
            RelocationCause::Capacity,
        };
    }
    return TrimIntent{victim->endpoint, pressure.target_tier};
}

std::optional<MaintenanceAction>
BaselineFixedLruStoragePolicy::next_background_action(
    const BackgroundTickContext&,
    const StorageView&
) const {
    return std::nullopt;
}

std::optional<MaintenanceAction> BaselineFixedLruStoragePolicy::on_storage_access(
    const StorageAccessContext&,
    const StorageView&
) const {
    return std::nullopt;
}

void BaselineFixedLruStoragePolicy::on_commit(
    const StorageMutation& mutation,
    const StorageView& storage_after_commit
) {
    state_.on_commit(mutation, storage_after_commit);
}

StoragePolicyStats BaselineFixedLruStoragePolicy::stats(const StorageView&) const {
    StoragePolicyStats result;
    result.slc_program_bytes = state_.program_bytes(StorageTier::Slc);
    result.tlc_program_bytes = state_.program_bytes(StorageTier::Tlc);
    return result;
}

}  // namespace dwpdsim
