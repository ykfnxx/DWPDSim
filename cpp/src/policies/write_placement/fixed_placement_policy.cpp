#include "dwpdsim/policies/write_placement/fixed_placement_policy.hpp"

namespace dwpdsim {

FixedPlacementPolicy::FixedPlacementPolicy(Medium medium, std::uint32_t stream_id)
    : placement_{medium, stream_id} {}

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

Placement FixedPlacementPolicy::place_on_medium(
    Medium medium,
    const Node& node,
    const AccessContext& context,
    const RadixTree& tree,
    const StorageSummary& storage
) {
    static_cast<void>(node);
    static_cast<void>(context);
    static_cast<void>(tree);
    static_cast<void>(storage);
    const std::uint32_t stream_id = medium == placement_.medium ? placement_.stream_id : 0;
    return Placement{medium, stream_id};
}

}  // namespace dwpdsim
