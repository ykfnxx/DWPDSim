#include "dwpdsim/trace_writer.hpp"

#include <ios>
#include <stdexcept>
#include <string_view>

namespace dwpdsim {
namespace {

std::string_view operation_name(Operation operation) noexcept {
    switch (operation) {
        case Operation::Read:
            return "READ";
        case Operation::Write:
            return "WRITE";
        case Operation::Trim:
            return "TRIM";
    }
    return "";
}

std::string_view storage_tier_name(StorageTier tier) noexcept {
    return tier == StorageTier::Slc ? "SLC" : "TLC";
}

std::string_view reason_name(TraceReason reason) noexcept {
    switch (reason) {
        case TraceReason::StorageHit:
            return "STORAGE_HIT";
        case TraceReason::MemoryEviction:
            return "MEMORY_EVICTION";
        case TraceReason::StorageEviction:
            return "STORAGE_EVICTION";
        case TraceReason::SlcDemotion:
            return "SLC_DEMOTION";
    }
    return "";
}

}  // namespace

TraceWriter::TraceWriter(
    const std::filesystem::path& path,
    std::uint64_t block_size_bytes
)
    : block_size_bytes_(block_size_bytes), buffer_(1U << 20U) {
    stream_.rdbuf()->pubsetbuf(buffer_.data(), static_cast<std::streamsize>(buffer_.size()));
    stream_.open(path, std::ios::out | std::ios::trunc);
    if (!stream_) {
        throw std::runtime_error("failed to open trace file: " + path.string());
    }
    stream_ << "sequence,timestamp,request_sequence,operation,storage_tier,stream_id,"
               "offset_bytes,length_bytes,node_id,hash_id,reason\n";
}

void TraceWriter::emit(
    const AccessContext& context,
    NodeId node_id,
    Operation operation,
    const Node& node,
    const StorageLocation& location,
    TraceReason reason
) {
    stream_ << next_sequence_++ << ',' << context.timestamp << ','
            << context.access_sequence << ',' << operation_name(operation) << ','
            << storage_tier_name(location.tier) << ',' << location.stream_id << ','
            << location.block_address * block_size_bytes_ << ',' << block_size_bytes_ << ','
            << node_id << ',' << node.hash_id << ',' << reason_name(reason) << '\n';
}

void TraceWriter::finish() {
    stream_.flush();
    if (!stream_) {
        throw std::runtime_error("failed to flush trace file");
    }
}

std::uint64_t TraceWriter::event_count() const noexcept {
    return next_sequence_;
}

}  // namespace dwpdsim
