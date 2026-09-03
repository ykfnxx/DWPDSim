#include "dwpdsim/metrics.hpp"

#include <algorithm>

namespace dwpdsim {

MetricsCollector::MetricsCollector(const SimulationConfig& config) {
    io[0].stream_writes.resize(config.slc.stream_count);
    io[1].stream_writes.resize(config.tlc.stream_count);
}

void MetricsCollector::record_request(TimestampNs timestamp_ns) noexcept {
    if (!start_timestamp_ns.has_value()) {
        start_timestamp_ns = timestamp_ns;
    }
    end_timestamp_ns = timestamp_ns;
    ++request_count;
}

void MetricsCollector::record_access(AccessResult result) noexcept {
    ++block_access_count;
    switch (result) {
        case AccessResult::MemoryHit:
            ++memory_hits;
            break;
        case AccessResult::SlcHit:
            ++slc_hits;
            break;
        case AccessResult::TlcHit:
            ++tlc_hits;
            break;
        case AccessResult::GlobalMiss:
            ++global_misses;
            break;
    }
}

void MetricsCollector::record_io(
    Operation operation,
    StorageTier tier,
    std::uint32_t stream_id,
    bool host_write
) noexcept {
    StorageTierIoCounters& counters = io[storage_tier_index(tier)];
    switch (operation) {
        case Operation::Read:
            ++counters.reads;
            break;
        case Operation::Write:
            ++counters.writes;
            ++counters.stream_writes[stream_id];
            if (host_write) {
                ++counters.host_writes;
            }
            break;
        case Operation::Trim:
            ++counters.trims;
            break;
    }
}

void MetricsCollector::memory_inserted(bool has_storage_copy) noexcept {
    ++memory_resident_blocks;
    peak_memory_resident_blocks = std::max(
        peak_memory_resident_blocks,
        memory_resident_blocks
    );
    if (has_storage_copy) {
        ++duplicated_blocks;
    }
}

void MetricsCollector::memory_removed(bool has_storage_copy) noexcept {
    --memory_resident_blocks;
    if (has_storage_copy) {
        --duplicated_blocks;
    }
}

void MetricsCollector::storage_written(StorageTier tier, bool has_memory_copy) noexcept {
    const std::size_t index = storage_tier_index(tier);
    ++storage_resident_blocks[index];
    peak_storage_resident_blocks[index] = std::max(
        peak_storage_resident_blocks[index],
        storage_resident_blocks[index]
    );
    if (has_memory_copy) {
        ++duplicated_blocks;
    }
}

void MetricsCollector::storage_removed(StorageTier tier, bool has_memory_copy) noexcept {
    --storage_resident_blocks[storage_tier_index(tier)];
    if (has_memory_copy) {
        --duplicated_blocks;
    }
}

}  // namespace dwpdsim
