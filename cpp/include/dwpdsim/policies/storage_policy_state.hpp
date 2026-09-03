#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <utility>

#include "dwpdsim/policies/storage_policy.hpp"

namespace dwpdsim {

class StoragePolicyState {
  public:
    struct SegmentTime {
        NodeId endpoint;
        TimestampNs timestamp_ns;
    };

    void on_commit(const StorageMutation& mutation, const StorageView& storage);

    std::optional<SegmentTime> lru_leaf(
        StorageTier tier,
        NodeSpan protected_nodes,
        const StorageView& storage
    ) const;
    std::optional<SegmentTime> oldest_segment(
        StorageTier tier,
        const StorageView& storage
    ) const;
    TimestampNs segment_first_ns(
        NodeId endpoint,
        StorageTier tier,
        const StorageView& storage
    ) const;
    TimestampNs segment_last_ns(
        NodeId endpoint,
        StorageTier tier,
        const StorageView& storage
    ) const;

    std::uint64_t program_bytes(StorageTier tier) const noexcept;
    std::uint64_t committed_write_segments(StorageTier tier) const noexcept;

  private:
    struct Entry {
        StorageTier tier;
        TimestampNs first_ns;
        TimestampNs last_ns;
        AffinityId affinity_id;
    };

    std::map<NodeId, Entry> entries_;
    std::array<std::uint64_t, 2> program_bytes_{};
    std::array<std::uint64_t, 2> committed_write_segments_{};
};

}  // namespace dwpdsim
