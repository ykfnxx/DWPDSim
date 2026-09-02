#pragma once

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <vector>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

class TraceWriter {
  public:
    TraceWriter(const std::filesystem::path& path, std::uint64_t block_size_bytes);

    void emit(
        const AccessContext& context,
        Operation operation,
        const Node& node,
        const StorageLocation& location,
        TraceReason reason
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
