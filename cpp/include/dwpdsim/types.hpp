#pragma once

#include <cstdint>
#include <limits>
#include <optional>

namespace dwpdsim {

using HashId = std::uint64_t;
using NodeId = std::uint64_t;
using Timestamp = std::uint64_t;

inline constexpr NodeId kRootNodeId = 0;
inline constexpr NodeId kInvalidNodeId = std::numeric_limits<NodeId>::max();

enum class Medium : std::uint8_t {
    Slc,
    Tlc,
};

struct StorageLocation {
    Medium medium;
    std::uint64_t block_address;
    std::uint32_t stream_id;
};

struct Node {
    NodeId parent_id = kInvalidNodeId;
    HashId hash_id = 0;
    Timestamp first_seen_timestamp = 0;
    Timestamp last_access_timestamp = 0;
    std::optional<Timestamp> last_hit_timestamp;
    std::uint64_t access_count = 0;
    bool in_memory = false;
    std::optional<StorageLocation> storage_location;
};

}  // namespace dwpdsim
