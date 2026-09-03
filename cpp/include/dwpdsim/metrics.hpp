#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/types.hpp"

namespace dwpdsim {

struct MediumIoCounters {
    std::uint64_t reads = 0;
    std::uint64_t writes = 0;
    std::uint64_t trims = 0;
    std::vector<std::uint64_t> stream_writes;
};

class MetricsCollector {
  public:
    explicit MetricsCollector(const SimulationConfig& config);

    void record_request(Timestamp timestamp) noexcept;
    void record_access(AccessResult result) noexcept;
    void record_io(Operation operation, Medium medium, std::uint32_t stream_id) noexcept;
    void memory_inserted(bool has_storage_copy) noexcept;
    void memory_removed(bool has_storage_copy) noexcept;
    void storage_written(Medium medium, bool has_memory_copy) noexcept;
    void storage_removed(Medium medium, bool has_memory_copy) noexcept;

    std::uint64_t request_count = 0;
    std::uint64_t block_access_count = 0;
    std::uint64_t memory_hits = 0;
    std::uint64_t slc_hits = 0;
    std::uint64_t tlc_hits = 0;
    std::uint64_t global_misses = 0;

    std::uint64_t storage_promotions = 0;
    std::uint64_t storage_bypasses = 0;
    std::uint64_t memory_evicted_segments = 0;
    std::uint64_t memory_evictions = 0;
    std::uint64_t memory_evictions_with_storage_copy = 0;
    std::uint64_t memory_eviction_drops = 0;
    std::uint64_t memory_eviction_persists = 0;

    std::uint64_t memory_resident_blocks = 0;
    std::uint64_t peak_memory_resident_blocks = 0;
    std::uint64_t duplicated_blocks = 0;

    std::array<std::uint64_t, 2> storage_resident_blocks{};
    std::array<std::uint64_t, 2> peak_storage_resident_blocks{};
    std::array<std::uint64_t, 2> storage_evicted_segments{};
    std::array<std::uint64_t, 2> storage_evicted_blocks{};
    std::array<MediumIoCounters, 2> io;

    std::uint64_t tree_nodes_created = 0;
    std::uint64_t tree_nodes_removed = 0;

    std::optional<Timestamp> start_timestamp;
    std::optional<Timestamp> end_timestamp;

};

}  // namespace dwpdsim
