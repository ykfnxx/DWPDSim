#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>

namespace dwpdsim {

using HashId = std::uint64_t;
using NodeId = HashId;
using NodeSlot = std::uint32_t;
using Timestamp = std::uint64_t;

inline constexpr NodeSlot kInvalidNodeSlot = std::numeric_limits<NodeSlot>::max();

enum class StorageTier : std::uint8_t {
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

enum class StorageEvictionAction : std::uint8_t {
    Drop,
    DemoteToTlc,
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
    SlcDemotion,
};

struct StorageLocation {
    StorageTier tier;
    std::uint64_t block_address;
    std::uint32_t stream_id;
};

struct Node {
    HashId hash_id = 0;
    Timestamp first_seen_timestamp = 0;
    Timestamp last_access_timestamp = 0;
    Timestamp last_hit_timestamp = 0;
    std::uint64_t access_count = 0;
    std::uint64_t storage_block_address = 0;
    std::uint32_t storage_stream_id = 0;
    StorageTier storage_tier = StorageTier::Slc;
    bool in_memory = false;
    bool on_storage = false;
    bool has_last_hit = false;

    StorageLocation storage_location() const noexcept {
        return StorageLocation{storage_tier, storage_block_address, storage_stream_id};
    }

    void set_storage_location(StorageLocation location) noexcept {
        storage_block_address = location.block_address;
        storage_stream_id = location.stream_id;
        storage_tier = location.tier;
        on_storage = true;
    }

    void clear_storage_location() noexcept {
        on_storage = false;
    }
};

struct AccessContext {
    Timestamp timestamp;
    std::uint64_t access_sequence;
    std::uint64_t request_index;
    std::uint64_t position;
    NodeId node_id;
    std::optional<NodeId> parent_id;
};

struct Placement {
    StorageTier tier;
    std::uint32_t stream_id;
};

constexpr std::size_t storage_tier_index(StorageTier tier) noexcept {
    return tier == StorageTier::Slc ? 0U : 1U;
}

}  // namespace dwpdsim
