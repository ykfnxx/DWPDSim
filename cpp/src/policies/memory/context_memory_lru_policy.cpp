#include "dwpdsim/policies/memory/context_memory_lru_policy.hpp"

#include <cassert>
#include <stdexcept>
#include <tuple>

#include "dwpdsim/radix_tree.hpp"

namespace dwpdsim {

ContextMemoryLruPolicy::ContextMemoryLruPolicy(
    bool admit_storage_hits, std::uint64_t capacity_blocks, double alpha,
    std::optional<TimestampNs> retention_ns
) : IndexedMemoryLruPolicy(admit_storage_hits, 1, 0, 1, 0, retention_ns),
    candidate_block_budget_(alpha * static_cast<double>(capacity_blocks)) {
    if (!(alpha > 0.0 && alpha <= 1.0)) {
        throw std::invalid_argument("context_lru alpha must be in (0, 1]");
    }
}

NodeId ContextMemoryLruPolicy::select_victim() const {
    using Score = std::tuple<std::uint64_t, std::size_t, Key>;
    std::optional<Score> best;
    std::uint64_t covered = 0;
    for (const Key& key : groups_.front()) {
        ++work_.candidates_examined;
        const NodeId endpoint = key.second;
        const auto count = subtree_memory_.find(endpoint);
        const auto subtree = count == subtree_memory_.end() ? 0 : count->second;
        if (subtree != residents_.count(endpoint)) { continue; }

        const auto blocks = segments_.at(endpoints_.at(endpoint)).members.size();
        std::uint64_t depth = 0;
        for (std::optional<NodeId> node = endpoint; node; node = tree_->parent(*node)) {
            ++depth;
        }
        const Score score{depth, blocks, key};
        if (!best || score < *best) { best = score; }
        covered += blocks;
        // Whole segments enter the candidate set, including the one crossing the budget.
        if (static_cast<double>(covered) >= candidate_block_budget_) { break; }
    }
    assert(best.has_value());
    return std::get<2>(*best).second;
}

}  // namespace dwpdsim
