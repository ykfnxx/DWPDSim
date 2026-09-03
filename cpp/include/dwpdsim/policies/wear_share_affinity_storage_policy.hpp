#pragma once

#include "dwpdsim/policies/storage_policy.hpp"
#include "dwpdsim/policies/storage_policy_state.hpp"

namespace dwpdsim {

struct WearShareAffinityPolicyConfig {
    double slc_host_share = 0.68;
    double logical_fill_fraction = 0.98;
};

class WearShareAffinityStoragePolicy final : public StoragePolicy {
  public:
    explicit WearShareAffinityStoragePolicy(WearShareAffinityPolicyConfig config);

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
    static std::uint64_t stable_hash(std::uint64_t value);
    StorageTier choose_tier(const DumpContext& dump) const;
    std::uint32_t stream_for(
        StorageTier tier,
        AffinityId affinity_id,
        NodeId segment_endpoint,
        const StorageView& storage
    ) const;

    WearShareAffinityPolicyConfig config_;
    StoragePolicyState state_;
};

}  // namespace dwpdsim
