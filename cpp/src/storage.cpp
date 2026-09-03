#include "dwpdsim/storage.hpp"

#include <algorithm>
#include <cstddef>

namespace dwpdsim {

StorageTierState::StorageTierState(
    const StorageTierConfig& config,
    std::uint64_t block_size_bytes
)
    : capacity_blocks_(config.capacity_bytes / block_size_bytes),
      stream_count_(config.stream_count) {}

bool StorageTierState::full() const noexcept {
    return used_blocks_ == capacity_blocks_;
}

std::uint64_t StorageTierState::allocate() noexcept {
    std::uint64_t address;
    if (recycled_addresses_.empty()) {
        address = next_address_++;
    } else {
        address = recycled_addresses_.back();
        recycled_addresses_.pop_back();
    }
    ++used_blocks_;
    peak_used_blocks_ = std::max(peak_used_blocks_, used_blocks_);
    return address;
}

void StorageTierState::release(std::uint64_t block_address) noexcept {
    recycled_addresses_.push_back(block_address);
    --used_blocks_;
}

std::uint64_t StorageTierState::capacity_blocks() const noexcept {
    return capacity_blocks_;
}

std::uint64_t StorageTierState::used_blocks() const noexcept {
    return used_blocks_;
}

std::uint64_t StorageTierState::peak_used_blocks() const noexcept {
    return peak_used_blocks_;
}

std::uint32_t StorageTierState::stream_count() const noexcept {
    return stream_count_;
}

StorageState::StorageState(const SimulationConfig& config)
    : tiers_{
          StorageTierState(config.slc, config.block_size_bytes),
          StorageTierState(config.tlc, config.block_size_bytes),
      } {}

StorageTierState& StorageState::tier(StorageTier tier) noexcept {
    return tiers_[storage_tier_index(tier)];
}

const StorageTierState& StorageState::tier(StorageTier tier) const noexcept {
    return tiers_[storage_tier_index(tier)];
}

StorageSummary StorageState::summary() const noexcept {
    const StorageTierState& slc = tiers_[0];
    const StorageTierState& tlc = tiers_[1];
    return StorageSummary{
        StorageTierSummary{slc.capacity_blocks(), slc.used_blocks(), slc.stream_count()},
        StorageTierSummary{tlc.capacity_blocks(), tlc.used_blocks(), tlc.stream_count()},
    };
}

}  // namespace dwpdsim
