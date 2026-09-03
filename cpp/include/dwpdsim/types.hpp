#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>

namespace dwpdsim {

using HashId = std::uint64_t;
using NodeId = HashId;
using NodeSlot = std::uint32_t;
using TimestampNs = std::uint64_t;
using RequestId = std::uint64_t;
using AffinityId = std::uint64_t;
using MoveId = std::uint64_t;

inline constexpr NodeSlot kInvalidNodeSlot = std::numeric_limits<NodeSlot>::max();

template <typename T>
struct ReadOnlySpan {
    const T* data = nullptr;
    std::size_t size = 0;

    const T* begin() const noexcept { return data; }
    const T* end() const noexcept { return size == 0 ? data : data + size; }
    const T& operator[](std::size_t index) const noexcept { return data[index]; }
    bool empty() const noexcept { return size == 0; }
};

using HashSpan = ReadOnlySpan<HashId>;
using NodeSpan = ReadOnlySpan<NodeId>;

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

enum class MemoryEvictionAction : std::uint8_t {
    Drop,
    Dump,
};

enum class Operation : std::uint8_t {
    Read,
    Write,
    Trim,
};

enum class TraceReason : std::uint8_t {
    StorageHit,
    MemoryDump,
    CapacityEviction,
    IdleEviction,
    AccessMigration,
    BackgroundMigration,
};

enum class CapacityCause : std::uint8_t {
    DumpAdmission,
    BackgroundMigration,
    AccessMigration,
};

enum class RelocationCause : std::uint8_t {
    Capacity,
    Access,
    Background,
};

struct StorageLocation {
    StorageTier tier = StorageTier::Slc;
    std::uint64_t block_address = 0;
    std::uint32_t stream_id = 0;
};

inline bool operator==(const StorageLocation& lhs, const StorageLocation& rhs) noexcept {
    return lhs.tier == rhs.tier && lhs.block_address == rhs.block_address &&
           lhs.stream_id == rhs.stream_id;
}

struct Node {
    HashId hash_id = 0;
    TimestampNs first_seen_timestamp_ns = 0;
    TimestampNs last_access_timestamp_ns = 0;
    TimestampNs last_hit_timestamp_ns = 0;
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

    void clear_storage_location() noexcept { on_storage = false; }
};

struct RequestContext {
    TimestampNs timestamp_ns = 0;
    RequestId request_id = 0;
    AffinityId affinity_id = 0;
    HashSpan ordered_hashes;
    NodeSpan protected_prefix;
};

struct AccessContext {
    RequestContext request;
    std::uint64_t access_sequence = 0;
    std::uint64_t position = 0;
    NodeId node_id = 0;
    std::optional<NodeId> parent_id;
};

struct Placement {
    StorageTier tier = StorageTier::Slc;
    std::uint32_t stream_id = 0;
};

inline bool operator==(const Placement& lhs, const Placement& rhs) noexcept {
    return lhs.tier == rhs.tier && lhs.stream_id == rhs.stream_id;
}

constexpr std::size_t storage_tier_index(StorageTier tier) noexcept {
    return tier == StorageTier::Slc ? 0U : 1U;
}

}  // namespace dwpdsim
