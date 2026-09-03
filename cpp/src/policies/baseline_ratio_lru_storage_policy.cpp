#include "dwpdsim/policies/baseline_ratio_lru_storage_policy.hpp"

namespace dwpdsim {

BaselineRatioLruStoragePolicy::BaselineRatioLruStoragePolicy(double slc_write_ratio)
    : slc_write_ratio_(slc_write_ratio) {}

BackgroundSchedule BaselineRatioLruStoragePolicy::background_schedule() const {
    return {};
}

void BaselineRatioLruStoragePolicy::on_request_begin(
    const RequestContext&,
    const StorageView&
) {}

DumpPlacementDecision BaselineRatioLruStoragePolicy::place_dump(
    const DumpContext& dump,
    const StorageView& storage
) const {
    const std::uint64_t slc = state_.program_bytes(StorageTier::Slc);
    const std::uint64_t tlc = state_.program_bytes(StorageTier::Tlc);
    const double target_slc = static_cast<double>(slc + tlc + dump.write_bytes) *
                              slc_write_ratio_;
    const StorageTier tier = static_cast<double>(slc) < target_slc
                                 ? StorageTier::Slc
                                 : StorageTier::Tlc;
    return DumpPlacementDecision{Placement{tier, next_stream(tier, storage)}};
}

std::uint64_t BaselineRatioLruStoragePolicy::capacity_limit_blocks(
    StorageTier tier,
    const StorageView& storage
) const {
    return storage.storage().tier(tier).capacity_blocks();
}

std::optional<CapacityAction> BaselineRatioLruStoragePolicy::reclaim_for(
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
        return RelocateIntent{
            victim->endpoint,
            Placement{StorageTier::Tlc, next_stream(StorageTier::Tlc, storage)},
            RelocationCause::Capacity,
        };
    }
    return TrimIntent{victim->endpoint, pressure.target_tier};
}

std::optional<MaintenanceAction>
BaselineRatioLruStoragePolicy::next_background_action(
    const BackgroundTickContext&,
    const StorageView&
) const {
    return std::nullopt;
}

std::optional<MaintenanceAction> BaselineRatioLruStoragePolicy::on_storage_access(
    const StorageAccessContext&,
    const StorageView&
) const {
    return std::nullopt;
}

void BaselineRatioLruStoragePolicy::on_commit(
    const StorageMutation& mutation,
    const StorageView& storage_after_commit
) {
    state_.on_commit(mutation, storage_after_commit);
}

StoragePolicyStats BaselineRatioLruStoragePolicy::stats(const StorageView&) const {
    StoragePolicyStats result;
    result.slc_program_bytes = state_.program_bytes(StorageTier::Slc);
    result.tlc_program_bytes = state_.program_bytes(StorageTier::Tlc);
    return result;
}

std::uint32_t BaselineRatioLruStoragePolicy::next_stream(
    StorageTier tier,
    const StorageView& storage
) const {
    const std::uint32_t count = storage.storage().tier(tier).stream_count();
    return static_cast<std::uint32_t>(state_.committed_write_segments(tier) % count);
}

}  // namespace dwpdsim
