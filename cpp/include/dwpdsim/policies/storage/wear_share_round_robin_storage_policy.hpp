#pragma once

#include <array>
#include <set>
#include <string>
#include <unordered_map>

#include "dwpdsim/policies/storage/storage_policy.hpp"
#include "dwpdsim/policies/storage/storage_policy_state.hpp"

namespace dwpdsim {

struct WearShareRoundRobinPolicyConfig {
    double slc_host_share = 0.405;
    double logical_fill_fraction = 0.98;
    std::string victim_search = "indexed";  // scan, fused, indexed
    bool subtree_counts = false;
    bool verify_victims = false;  // Compare each decision with the original scan.
    bool profile = false;
};

class WearShareRoundRobinStoragePolicy final : public StoragePolicy {
  public:
    explicit WearShareRoundRobinStoragePolicy(
        WearShareRoundRobinPolicyConfig config
    );

    void on_node_created(NodeId node_id, const StorageView& storage) override;
    void on_node_pruned(
        NodeId node_id, std::optional<NodeId> parent, const StorageView& storage
    ) override;
    StoragePolicyWork work() const override;

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

    using Key = std::pair<TimestampNs, NodeId>;
    struct Segment {
        std::array<std::uint64_t, 2> resident_blocks{};
        std::array<TimestampNs, 2> last_ns{};
    };

    void erase_segment(NodeId endpoint);
    void refresh_segment(NodeId endpoint, const RadixTree& tree);
    void change_ancestors(NodeId node_id, bool insert, const RadixTree& tree);
    std::optional<StoragePolicyState::SegmentTime> indexed_victim(
        const CapacityPressureContext& pressure, const StorageView& storage
    ) const;

    WearShareRoundRobinPolicyConfig config_;
    bool indexed_;
    // All resident segments are ordered; leaf/protection eligibility is checked at query time.
    std::unordered_map<NodeId, Segment> segments_;
    std::array<std::set<Key>, 2> candidates_;
    std::unordered_map<NodeId, std::uint64_t> subtree_storage_;
    std::array<std::uint64_t, 2> program_bytes_{};
    std::array<std::uint64_t, 2> write_segments_{};
    std::vector<NodeId> scratch_;
    mutable StoragePolicyWork work_;
    StoragePolicyState state_;
};

}  // namespace dwpdsim
