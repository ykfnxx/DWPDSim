#pragma once

#include <array>
#include <map>

#include "dwpdsim/policies/storage_policy.hpp"
#include "dwpdsim/policies/storage_policy_state.hpp"

namespace dwpdsim {

struct AdaptiveEndurancePolicyConfig {
    double idle_multiplier = 32.0;
    double promotion_seconds = 14400.0;
    double adaptation_gain = 2.0;
    double direct_gain = 1.0;
    double slc_soft_utilization = 0.75;
    double occupancy_decay = 8.0;
    double logical_fill_fraction = 0.98;
    double slc_erase_budget = 120.0;
    double tlc_erase_budget = 12.0;
    TimestampNs background_period_ns = 900ULL * 1000ULL * 1000ULL * 1000ULL;
};

class AdaptiveEnduranceStoragePolicy final : public StoragePolicy {
  public:
    explicit AdaptiveEnduranceStoragePolicy(AdaptiveEndurancePolicyConfig config);

    BackgroundSchedule background_schedule() const override;
    void on_request_begin(
        const RequestContext& request,
        const StorageView& storage
    ) override;
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
    class GapEstimator {
      public:
        GapEstimator();
        void observe(double gap_seconds);
        double q95() const noexcept;
        std::uint64_t samples() const noexcept;

      private:
        void recompute();

        std::array<double, 64> edges_{};
        std::array<double, 64> counts_{};
        double cached_q95_ = 257.2775;
        std::uint64_t samples_ = 0;
    };

    static std::uint64_t stable_hash(std::uint64_t value, std::uint64_t salt);
    StorageTier choose_tier(const DumpContext& dump, const StorageView& storage) const;
    std::uint32_t stream_for(
        StorageTier tier,
        AffinityId affinity_id,
        NodeId segment_endpoint,
        const StorageView& storage
    ) const;
    double idle_threshold_seconds(const StorageView& storage) const;
    double effective_promotion_seconds(const StorageView& storage) const;

    AdaptiveEndurancePolicyConfig config_;
    StoragePolicyState state_;
    GapEstimator gaps_;
    std::map<AffinityId, TimestampNs> session_last_request_ns_;
};

}  // namespace dwpdsim
