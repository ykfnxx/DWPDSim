#include "dwpdsim/storage.hpp"

#include <algorithm>
#include <cstddef>

namespace dwpdsim {

MediumState::MediumState(const MediumConfig& config, std::uint64_t block_size_bytes)
    : capacity_blocks_(config.capacity_bytes / block_size_bytes),
      stream_count_(config.stream_count) {}

bool MediumState::full() const noexcept {
    return used_blocks_ == capacity_blocks_;
}

std::uint64_t MediumState::allocate() noexcept {
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

void MediumState::release(std::uint64_t block_address) noexcept {
    recycled_addresses_.push_back(block_address);
    --used_blocks_;
}

std::uint64_t MediumState::capacity_blocks() const noexcept {
    return capacity_blocks_;
}

std::uint64_t MediumState::used_blocks() const noexcept {
    return used_blocks_;
}

std::uint64_t MediumState::peak_used_blocks() const noexcept {
    return peak_used_blocks_;
}

std::uint32_t MediumState::stream_count() const noexcept {
    return stream_count_;
}

StorageState::StorageState(const SimulationConfig& config)
    : media_{
          MediumState(config.slc, config.block_size_bytes),
          MediumState(config.tlc, config.block_size_bytes),
      } {}

MediumState& StorageState::medium(Medium medium) noexcept {
    return media_[medium_index(medium)];
}

const MediumState& StorageState::medium(Medium medium) const noexcept {
    return media_[medium_index(medium)];
}

StorageSummary StorageState::summary() const noexcept {
    const MediumState& slc = media_[0];
    const MediumState& tlc = media_[1];
    return StorageSummary{
        MediumSummary{slc.capacity_blocks(), slc.used_blocks(), slc.stream_count()},
        MediumSummary{tlc.capacity_blocks(), tlc.used_blocks(), tlc.stream_count()},
    };
}

}  // namespace dwpdsim
