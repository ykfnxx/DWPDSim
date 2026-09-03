#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/types.hpp"

namespace dwpdsim {

struct StorageTierIoCounters {
    std::uint64_t reads = 0;
    std::uint64_t writes = 0;
    std::uint64_t trims = 0;
    std::uint64_t host_writes = 0;
    std::vector<std::uint64_t> stream_writes;
};

struct SegmentBlockByteCounters {
    std::uint64_t segments = 0;
    std::uint64_t blocks = 0;
    std::uint64_t bytes = 0;
};

class MetricsCollector {
  public:
    explicit MetricsCollector(const SimulationConfig& config);

    void record_request(TimestampNs timestamp_ns) noexcept;
    void record_access(AccessResult result) noexcept;
    void record_io(
        Operation operation,
        StorageTier tier,
        std::uint32_t stream_id,
        bool host_write = false
    ) noexcept;
    void memory_inserted(bool has_storage_copy) noexcept;
    void memory_removed(bool has_storage_copy) noexcept;
    void storage_written(StorageTier tier, bool has_memory_copy) noexcept;
    void storage_removed(StorageTier tier, bool has_memory_copy) noexcept;

    std::uint64_t request_count = 0;
    std::uint64_t block_access_count = 0;
    std::uint64_t memory_hits = 0;
    std::uint64_t slc_hits = 0;
    std::uint64_t tlc_hits = 0;
    std::uint64_t global_misses = 0;

    std::uint64_t storage_promotions = 0;
    std::uint64_t storage_bypasses = 0;
    std::uint64_t memory_evicted_segments = 0;
    std::uint64_t memory_evicted_blocks = 0;
    std::uint64_t memory_evictions_with_storage_copy = 0;
    std::uint64_t memory_drop_segments = 0;
    std::uint64_t memory_drop_blocks = 0;
    std::uint64_t memory_dump_segments = 0;
    std::uint64_t memory_dump_blocks = 0;

    std::uint64_t dump_requests = 0;
    SegmentBlockByteCounters dumps_admitted;
    SegmentBlockByteCounters dumps_rejected;
    SegmentBlockByteCounters foreground_capacity_evictions;
    std::uint64_t background_ticks = 0;
    SegmentBlockByteCounters background_idle_evictions;
    SegmentBlockByteCounters access_migrations;
    SegmentBlockByteCounters background_migrations;
    SegmentBlockByteCounters capacity_migrations;

    std::uint64_t relocation_source_read_blocks = 0;
    std::uint64_t relocation_source_read_bytes = 0;
    std::uint64_t relocation_reused_read_blocks = 0;
    std::uint64_t relocation_reused_read_bytes = 0;
    std::uint64_t relocation_explicit_read_blocks = 0;
    std::uint64_t relocation_explicit_read_bytes = 0;
    std::uint64_t relocation_destination_write_blocks = 0;
    std::uint64_t relocation_destination_write_bytes = 0;
    std::uint64_t relocation_source_trim_blocks = 0;
    std::uint64_t relocation_source_trim_bytes = 0;

    std::array<SegmentBlockByteCounters, 2> placements;
    std::uint64_t no_space = 0;
    std::uint64_t protected_victim_exhaustion = 0;
    std::uint64_t admission_rejections = 0;

    std::uint64_t memory_resident_blocks = 0;
    std::uint64_t peak_memory_resident_blocks = 0;
    std::uint64_t duplicated_blocks = 0;
    std::array<std::uint64_t, 2> storage_resident_blocks{};
    std::array<std::uint64_t, 2> peak_storage_resident_blocks{};
    std::array<StorageTierIoCounters, 2> io;

    std::uint64_t tree_nodes_created = 0;
    std::uint64_t tree_nodes_removed = 0;
    std::optional<TimestampNs> start_timestamp_ns;
    std::optional<TimestampNs> end_timestamp_ns;
};

}  // namespace dwpdsim
