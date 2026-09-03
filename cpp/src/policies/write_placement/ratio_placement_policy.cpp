#include "dwpdsim/policies/write_placement/ratio_placement_policy.hpp"

#include <cstddef>

namespace dwpdsim {

RatioPlacementPolicy::RatioPlacementPolicy(
    double slc_ratio,
    std::uint32_t slc_stream_count,
    std::uint32_t tlc_stream_count
)
    : slc_ratio_(slc_ratio), stream_counts_{slc_stream_count, tlc_stream_count} {}

Placement RatioPlacementPolicy::place(
    const Node& node,
    const AccessContext& context,
    const RadixTree& tree,
    const StorageSummary& storage
) {
    static_cast<void>(node);
    static_cast<void>(context);
    static_cast<void>(tree);
    static_cast<void>(storage);

    const std::uint64_t next_total = write_counts_[0] + write_counts_[1] + 1;
    const double target_slc_writes = static_cast<double>(next_total) * slc_ratio_;
    const StorageTier tier = static_cast<double>(write_counts_[0]) < target_slc_writes
                              ? StorageTier::Slc
                              : StorageTier::Tlc;
    ++write_counts_[storage_tier_index(tier)];
    return next_placement(tier);
}

Placement RatioPlacementPolicy::place_on_tier(
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
    return next_placement(tier);
}

Placement RatioPlacementPolicy::next_placement(StorageTier tier) {
    const std::size_t index = storage_tier_index(tier);
    const std::uint32_t stream_id = next_stream_[index] % stream_counts_[index];
    ++next_stream_[index];
    return Placement{tier, stream_id};
}

}  // namespace dwpdsim
