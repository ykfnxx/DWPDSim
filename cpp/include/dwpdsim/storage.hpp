#pragma once

#include <array>
#include <cstdint>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/types.hpp"

namespace dwpdsim {

struct StorageTierSummary {
    std::uint64_t capacity_blocks = 0;
    std::uint64_t used_blocks = 0;
    std::uint32_t stream_count = 0;
};

struct StorageSummary {
    StorageTierSummary slc;
    StorageTierSummary tlc;
};

class StorageTierState {
  public:
    StorageTierState() = default;
    StorageTierState(const StorageTierConfig& config, std::uint64_t block_size_bytes);

    bool has_free_blocks(std::uint64_t blocks) const noexcept;
    std::uint64_t allocate() noexcept;
    void release(std::uint64_t block_address) noexcept;

    std::uint64_t capacity_blocks() const noexcept;
    std::uint64_t used_blocks() const noexcept;
    std::uint64_t free_blocks() const noexcept;
    std::uint64_t peak_used_blocks() const noexcept;
    std::uint32_t stream_count() const noexcept;

  private:
    std::uint64_t capacity_blocks_ = 0;
    std::uint64_t used_blocks_ = 0;
    std::uint64_t peak_used_blocks_ = 0;
    std::uint64_t next_address_ = 0;
    std::uint32_t stream_count_ = 0;
    std::vector<std::uint64_t> recycled_addresses_;
};

class StorageState {
  public:
    explicit StorageState(const SimulationConfig& config);

    StorageTierState& tier(StorageTier tier) noexcept;
    const StorageTierState& tier(StorageTier tier) const noexcept;
    StorageSummary summary() const noexcept;

  private:
    std::array<StorageTierState, 2> tiers_;
};

}  // namespace dwpdsim
