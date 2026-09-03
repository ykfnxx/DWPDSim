#pragma once

#include <cstdint>
#include <string>

namespace dwpdsim {

struct StorageTierConfig {
    std::uint64_t capacity_bytes;
    std::uint32_t stream_count;
};

struct SimulationConfig {
    std::uint64_t block_size_bytes = 8ULL * 1024ULL * 1024ULL;
    std::uint64_t memory_capacity_bytes = 0;
    StorageTierConfig slc{};
    StorageTierConfig tlc{};
    std::string timestamp_unit = "unspecified";
    std::uint64_t progress_interval_requests = 0;
};

}  // namespace dwpdsim
