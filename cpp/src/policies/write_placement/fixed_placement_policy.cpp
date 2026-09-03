#include "dwpdsim/policies/write_placement/fixed_placement_policy.hpp"

namespace dwpdsim {

FixedPlacementPolicy::FixedPlacementPolicy(StorageTier tier, std::uint32_t stream_id)
    : placement_{tier, stream_id} {}

Placement FixedPlacementPolicy::place(
    const Node& node,
    const AccessContext& context,
    const RadixTree& tree,
    const StorageSummary& storage
) {
    static_cast<void>(node);
    static_cast<void>(context);
    static_cast<void>(tree);
    static_cast<void>(storage);
    return placement_;
}

Placement FixedPlacementPolicy::place_on_tier(
    StorageTier tier,
    const Node& node,
    const AccessContext& context,
    const RadixTree& tree,
    const StorageSummary& storage
) {
    static_cast<void>(node);
    static_cast<void>(context);
    static_cast<void>(tree);
    static_cast<void>(storage);
    const std::uint32_t stream_id = tier == placement_.tier ? placement_.stream_id : 0;
    return Placement{tier, stream_id};
}

}  // namespace dwpdsim
