#pragma once

#include "dwpdsim/policies/memory/indexed_memory_lru_policy.hpp"

namespace dwpdsim {

// Prefer shallow endpoints within a capacity-bounded, globally oldest candidate set.
class ContextMemoryLruPolicy final : public IndexedMemoryLruPolicy {
  public:
    ContextMemoryLruPolicy(bool admit_storage_hits, std::uint64_t capacity_blocks,
                           double alpha = 0.01,
                           std::optional<TimestampNs> retention_ns = std::nullopt);

  private:
    NodeId select_victim() const override;
    double candidate_block_budget_;
};

}  // namespace dwpdsim
