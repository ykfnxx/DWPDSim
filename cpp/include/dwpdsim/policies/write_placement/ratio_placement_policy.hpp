#pragma once

#include <array>
#include <cstdint>

#include "dwpdsim/policies/write_placement/write_placement_policy_base.hpp"

namespace dwpdsim {

class RatioPlacementPolicy final : public WritePlacementPolicyBase {
  public:
    RatioPlacementPolicy(
        double slc_ratio,
        std::uint32_t slc_stream_count,
        std::uint32_t tlc_stream_count
    );

    Placement place(
        const Node& node,
        const AccessContext& context,
        const RadixTree& tree,
        const StorageSummary& storage
    ) override;

    Placement place_on_medium(
        Medium medium,
        const Node& node,
        const AccessContext& context,
        const RadixTree& tree,
        const StorageSummary& storage
    ) override;

  private:
    Placement next_placement(Medium medium);

    double slc_ratio_;
    std::array<std::uint64_t, 2> write_counts_{};
    std::array<std::uint32_t, 2> next_stream_{};
    std::array<std::uint32_t, 2> stream_counts_;
};

}  // namespace dwpdsim
