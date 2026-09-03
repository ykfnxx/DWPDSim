#pragma once

#include "dwpdsim/policies/storage_policy.hpp"
#include "dwpdsim/policies/storage_policy_state.hpp"

namespace dwpdsim {

class BaselineRatioLruStoragePolicy final : public StoragePolicy {
  public:
    explicit BaselineRatioLruStoragePolicy(double slc_write_ratio);

    BackgroundSchedule background_schedule() const override;
    void on_request_begin(const RequestContext&, const StorageView&) override;
    DumpPlacementDecision place_dump(
        const DumpContext& dump,
        const StorageView& storage
    ) const override;
    std::uint64_t capacity_limit_blocks(
        StorageTier tier,
        const StorageView& storage
    ) const override;
    std::optional<CapacityAction> reclaim_for(
        const CapacityPressureContext& pressure,
        const StorageView& storage
    ) const override;
    std::optional<MaintenanceAction> next_background_action(
        const BackgroundTickContext& tick,
        const StorageView& storage
    ) const override;
    std::optional<MaintenanceAction> on_storage_access(
        const StorageAccessContext& access,
        const StorageView& storage
    ) const override;
    void on_commit(
        const StorageMutation& mutation,
        const StorageView& storage_after_commit
    ) override;
    StoragePolicyStats stats(const StorageView& storage) const override;

  private:
    std::uint32_t next_stream(StorageTier tier, const StorageView& storage) const;

    double slc_write_ratio_;
    StoragePolicyState state_;
};

}  // namespace dwpdsim
