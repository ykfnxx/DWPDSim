#pragma once

#include <cstdint>
#include <optional>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

struct MemoryConfig {
    std::uint64_t capacity_bytes = 0;
};

struct StorageTierConfig {
    std::uint64_t capacity_bytes = 0;
    std::uint32_t stream_count = 0;
};

struct SimulationConfig {
    std::uint64_t block_size_bytes = 8ULL * 1024ULL * 1024ULL;
    MemoryConfig memory{};
    StorageTierConfig slc{};
    StorageTierConfig tlc{};
    std::optional<TimestampNs> simulation_end_ns;
    std::uint64_t progress_interval_requests = 0;
};

}  // namespace dwpdsim
