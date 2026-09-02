#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <string>

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

enum class AccessResult : std::uint8_t {
    MemoryHit,
    SlcHit,
    TlcHit,
    GlobalMiss,
};

enum class EvictionAction : std::uint8_t {
    Drop,
    Persist,
};

enum class Operation : std::uint8_t {
    Read,
    Write,
    Trim,
};

enum class TraceReason : std::uint8_t {
    StorageHit,
    MemoryEviction,
    StorageEviction,
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

struct AccessContext {
    Timestamp timestamp;
    std::uint64_t access_sequence;
    std::uint64_t request_index;
    std::uint64_t position;
    NodeId node_id;
    NodeId parent_id;
};

struct Placement {
    Medium medium;
    std::uint32_t stream_id;
};

constexpr std::size_t medium_index(Medium medium) noexcept {
    return medium == Medium::Slc ? 0U : 1U;
}

}  // namespace dwpdsim
