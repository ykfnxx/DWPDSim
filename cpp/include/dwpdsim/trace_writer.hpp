#pragma once

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <optional>
#include <vector>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

struct TraceContext {
    TimestampNs timestamp_ns = 0;
    std::optional<RequestId> request_id;
    std::optional<std::uint64_t> access_sequence;
};

class TraceWriter {
  public:
    TraceWriter(const std::filesystem::path& path, std::uint64_t block_size_bytes);

    std::uint64_t emit(
        const TraceContext& context,
        NodeId node_id,
        Operation operation,
        const Node& node,
        const StorageLocation& location,
        TraceReason reason,
        std::optional<MoveId> move_id = std::nullopt,
        std::optional<std::uint64_t> depends_on_sequence = std::nullopt
    );
    void finish();

    std::uint64_t event_count() const noexcept;

  private:
    std::uint64_t block_size_bytes_;
    std::uint64_t next_sequence_ = 0;
    std::vector<char> buffer_;
    std::ofstream stream_;
};

}  // namespace dwpdsim
