#pragma once

#include "dwpdsim/policies/storage_policy.hpp"
#include "dwpdsim/policies/storage_policy_state.hpp"

namespace dwpdsim {

struct WearShareRoundRobinPolicyConfig {
    double slc_host_share = 0.405;
    double logical_fill_fraction = 0.98;
};

class WearShareRoundRobinStoragePolicy final : public StoragePolicy {
  public:
    explicit WearShareRoundRobinStoragePolicy(
        WearShareRoundRobinPolicyConfig config
    );

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
    StorageTier choose_tier(const DumpContext& dump) const;
    std::uint32_t next_stream(StorageTier tier, const StorageView& storage) const;

    WearShareRoundRobinPolicyConfig config_;
    StoragePolicyState state_;
};

}  // namespace dwpdsim
