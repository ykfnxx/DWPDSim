#pragma once

#include "dwpdsim/policies/write_placement/write_placement_policy_base.hpp"

namespace dwpdsim {

class FixedPlacementPolicy final : public WritePlacementPolicyBase {
  public:
    explicit FixedPlacementPolicy(StorageTier tier, std::uint32_t stream_id = 0);

    Placement place(
        const Node& node,
        const AccessContext& context,
        const RadixTree& tree,
        const StorageSummary& storage
    ) override;

    Placement place_on_tier(
        StorageTier tier,
        const Node& node,
        const AccessContext& context,
        const RadixTree& tree,
        const StorageSummary& storage
    ) override;

  private:
    Placement placement_;
};

}  // namespace dwpdsim
