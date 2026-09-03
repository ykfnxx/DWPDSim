#pragma once

#include <cstdint>
#include <optional>
#include <variant>
#include <vector>

#include "dwpdsim/radix_tree.hpp"
#include "dwpdsim/storage.hpp"
#include "dwpdsim/types.hpp"

namespace dwpdsim {

struct SegmentView {
    NodeId segment_top = 0;
    NodeId segment_endpoint = 0;
    std::vector<NodeId> ordered_nodes;

    NodeSpan nodes() const noexcept {
        return NodeSpan{ordered_nodes.data(), ordered_nodes.size()};
    }
};

class StorageView {
  public:
    StorageView(
        const RadixTree& tree,
        const StorageState& storage,
        std::uint64_t block_size_bytes
    );

    SegmentView resolve_segment(NodeId endpoint) const;
    bool is_storage_leaf(NodeId endpoint) const;
    std::uint64_t resident_blocks(NodeId endpoint, StorageTier tier) const;
    std::vector<NodeId> resident_nodes(NodeId endpoint, StorageTier tier) const;
    bool intersects_protected(NodeId endpoint, NodeSpan protected_nodes) const;

    const RadixTree& tree() const noexcept;
    const StorageState& storage() const noexcept;
    std::uint64_t block_size_bytes() const noexcept;

  private:
    const RadixTree& tree_;
    const StorageState& storage_;
    std::uint64_t block_size_bytes_;
};

struct BackgroundSchedule {
    TimestampNs period_ns = 0;
};

struct DumpContext {
    RequestContext request;
    const SegmentView& segment;
    NodeSpan write_nodes;
    std::uint64_t write_blocks = 0;
    std::uint64_t write_bytes = 0;
    NodeSpan protected_nodes;
};

struct DumpPlacementDecision {
    Placement placement;
};

struct CapacityPressureContext {
    TimestampNs timestamp_ns = 0;
    CapacityCause cause = CapacityCause::DumpAdmission;
    StorageTier target_tier = StorageTier::Slc;
    std::uint64_t required_blocks = 0;
    NodeSpan protected_nodes;
};

struct BackgroundTickContext {
    TimestampNs timestamp_ns = 0;
};

struct StorageAccessContext {
    AccessContext access;
    StorageLocation source;
};

struct TrimIntent {
    NodeId segment_endpoint = 0;
    StorageTier tier = StorageTier::Slc;
};

struct RelocateIntent {
    NodeId source_segment_endpoint = 0;
    Placement destination;
    RelocationCause cause = RelocationCause::Background;
};

using StorageActionIntent = std::variant<TrimIntent, RelocateIntent>;
using CapacityAction = StorageActionIntent;
using MaintenanceAction = StorageActionIntent;

enum class StorageMutationKind : std::uint8_t {
    DumpWriteCommitted,
    CapacityTrimCommitted,
    IdleTrimCommitted,
    RelocationReadCommitted,
    RelocationWriteCommitted,
    RelocationSourceTrimCommitted,
    StorageAccessCommitted,
    NodePruned,
};

struct StorageMutation {
    StorageMutationKind kind = StorageMutationKind::StorageAccessCommitted;
    TimestampNs timestamp_ns = 0;
    AffinityId affinity_id = 0;
    NodeId segment_endpoint = 0;
    NodeSpan nodes;
    Placement placement;
    StorageTier source_tier = StorageTier::Slc;
    std::uint64_t bytes = 0;
};

struct StoragePolicyStats {
    std::uint64_t slc_program_bytes = 0;
    std::uint64_t tlc_program_bytes = 0;
    std::uint64_t gap_samples = 0;
    double gap_q95_seconds = 0.0;
    double idle_threshold_seconds = 0.0;
};

class StoragePolicy {
  public:
    virtual ~StoragePolicy() = default;

    virtual BackgroundSchedule background_schedule() const = 0;
    virtual void on_request_begin(
        const RequestContext& request,
        const StorageView& storage
    ) = 0;
    virtual DumpPlacementDecision place_dump(
        const DumpContext& dump,
        const StorageView& storage
    ) const = 0;
    virtual std::uint64_t capacity_limit_blocks(
        StorageTier tier,
        const StorageView& storage
    ) const = 0;
    virtual std::optional<CapacityAction> reclaim_for(
        const CapacityPressureContext& pressure,
        const StorageView& storage
    ) const = 0;
    virtual std::optional<MaintenanceAction> next_background_action(
        const BackgroundTickContext& tick,
        const StorageView& storage
    ) const = 0;
    virtual std::optional<MaintenanceAction> on_storage_access(
        const StorageAccessContext& access,
        const StorageView& storage
    ) const = 0;
    virtual void on_commit(
        const StorageMutation& mutation,
        const StorageView& storage_after_commit
    ) = 0;
    virtual StoragePolicyStats stats(const StorageView& storage) const = 0;
};

}  // namespace dwpdsim
