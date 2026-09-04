#pragma once

#include <cstdint>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

class RadixTree;

struct MemoryEvictionDecision {
    NodeId leaf_segment_endpoint = 0;
    MemoryEvictionAction action = MemoryEvictionAction::Dump;
};

enum class MemoryMutationKind : std::uint8_t {
    Inserted,
    Accessed,
    Removed,
};

struct MemoryMutation {
    MemoryMutationKind kind;
    NodeId node_id;
};

class MemoryPolicy {
  public:
    virtual ~MemoryPolicy() = default;

    virtual bool admit_storage_hit(
        const AccessContext& access,
        const Node& node,
        const RadixTree& tree
    ) const = 0;
    virtual MemoryEvictionDecision evict(
        const RequestContext& request,
        const RadixTree& tree
    ) const = 0;
    virtual void on_commit(const MemoryMutation& mutation) = 0;
};

}  // namespace dwpdsim
