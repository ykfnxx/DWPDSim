#pragma once

#include <array>
#include <cstdint>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/policies/write_placement/write_placement_policy_base.hpp"
#include "dwpdsim/types.hpp"

namespace dwpdsim {

class MediumState {
  public:
    MediumState() = default;
    MediumState(const MediumConfig& config, std::uint64_t block_size_bytes);

    bool full() const noexcept;
    std::uint64_t allocate() noexcept;
    void release(std::uint64_t block_address) noexcept;

    std::uint64_t capacity_blocks() const noexcept;
    std::uint64_t used_blocks() const noexcept;
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

    MediumState& medium(Medium medium) noexcept;
    const MediumState& medium(Medium medium) const noexcept;
    StorageSummary summary() const noexcept;

  private:
    std::array<MediumState, 2> media_;
};

}  // namespace dwpdsim
