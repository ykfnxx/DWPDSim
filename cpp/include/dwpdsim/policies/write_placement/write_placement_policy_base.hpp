#pragma once

#include <cstdint>
#include <optional>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

class RadixTree;

struct MediumSummary {
    std::uint64_t capacity_blocks;
    std::uint64_t used_blocks;
    std::uint32_t stream_count;
};

struct StorageSummary {
    MediumSummary slc;
    MediumSummary tlc;
};

class WritePlacementPolicyBase {
  public:
    virtual ~WritePlacementPolicyBase() = default;

    virtual Placement place(
        const Node& node,
        const AccessContext& context,
        const RadixTree& tree,
        const StorageSummary& storage
    ) = 0;

    virtual void on_node_created(
        NodeId,
        std::optional<NodeId>,
        const RadixTree&
    ) {}
    virtual void on_node_removed(
        NodeId,
        std::optional<NodeId>,
        const RadixTree&
    ) {}
    virtual void on_access_complete(
        const AccessContext&,
        AccessResult,
        const RadixTree&
    ) {}
};

using WritePlacementPolicy = WritePlacementPolicyBase;

}  // namespace dwpdsim
