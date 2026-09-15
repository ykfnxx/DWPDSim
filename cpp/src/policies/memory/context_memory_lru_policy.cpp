#include "dwpdsim/policies/memory/context_memory_lru_policy.hpp"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <stdexcept>
#include <tuple>

#include "dwpdsim/radix_tree.hpp"

namespace dwpdsim {

ContextMemoryLruPolicy::ContextMemoryLruPolicy(
    bool admit_storage_hits, std::uint64_t capacity_blocks, double alpha,
    std::optional<TimestampNs> retention_ns,
    std::optional<std::uint64_t> max_eviction_blocks,
    double retention_growth_seconds_per_block,
    std::optional<TimestampNs> eviction_gap_reference_ns,
    std::uint64_t eviction_base_blocks
) : IndexedMemoryLruPolicy(admit_storage_hits, 1, 0, 1, 0, retention_ns),
    candidate_block_budget_(alpha * static_cast<double>(capacity_blocks)),
    max_eviction_blocks_(max_eviction_blocks), retention_ns_(retention_ns),
    retention_growth_seconds_per_block_(retention_growth_seconds_per_block),
    eviction_gap_reference_ns_(eviction_gap_reference_ns),
    eviction_base_blocks_(eviction_base_blocks),
    adaptive_(retention_growth_seconds_per_block != 0.0 || eviction_gap_reference_ns.has_value()) {
    if (!std::isfinite(retention_growth_seconds_per_block) || retention_growth_seconds_per_block < 0) {
        throw std::invalid_argument("retention_growth_seconds_per_block must be finite and nonnegative");
    }
    if (retention_growth_seconds_per_block > 0 && !retention_ns) {
        throw std::invalid_argument("retention growth requires retention_ns");
    }
    if (eviction_gap_reference_ns &&
        (*eviction_gap_reference_ns == 0 || !max_eviction_blocks || eviction_base_blocks == 0)) {
        throw std::invalid_argument("adaptive eviction requires positive gap reference, base and max blocks");
    }
    if (max_eviction_blocks_ == 0) {
        throw std::invalid_argument("max_eviction_blocks must be positive or unset");
    }
    if (!(alpha > 0.0 && alpha <= 1.0)) {
        throw std::invalid_argument("context_lru alpha must be in (0, 1]");
    }
}

MemoryEvictionDecision ContextMemoryLruPolicy::evict(
    const RequestContext& request, const RadixTree& tree
) const {
    auto decision = IndexedMemoryLruPolicy::evict(request, tree);
    decision.reclaim_parent = false;
    decision.max_eviction_blocks = max_eviction_blocks_;
    if (adaptive_) {
        const auto& segment = segments_.at(endpoints_.at(decision.leaf_segment_endpoint));
        const NodeId newest = segment.members.rbegin()->second;
        const auto& history = access_history_.at(newest);
        if (retention_ns_) {
            const long double retention = static_cast<long double>(*retention_ns_) +
                retention_growth_seconds_per_block_ * 1.0e9L * history.growth_blocks;
            decision.action = request.timestamp_ns - tree.node(newest).last_access_timestamp_ns > retention
                                  ? MemoryEvictionAction::Drop : MemoryEvictionAction::Dump;
        }
        if (eviction_gap_reference_ns_) {
            const long double blocks = history.gap_ns
                ? static_cast<long double>(eviction_base_blocks_) * *history.gap_ns / *eviction_gap_reference_ns_
                : static_cast<long double>(eviction_base_blocks_);
            decision.max_eviction_blocks = static_cast<std::uint64_t>(
                std::clamp(blocks, 1.0L, static_cast<long double>(*max_eviction_blocks_)));
        }
    }
    return decision;
}

void ContextMemoryLruPolicy::on_request_begin(const RequestContext& request, const RadixTree&) {
    if (!adaptive_) { return; }
    active_request_id_ = request.request_id;
    active_timestamp_ns_ = request.timestamp_ns;
    active_growth_blocks_ = 0;
    if (retention_growth_seconds_per_block_ > 0 && request.affinity_id != 0) {
        const auto previous = sessions_.find(request.affinity_id);
        if (previous != sessions_.end()) { active_growth_blocks_ = previous->second.growth_blocks; }
    }
}

void ContextMemoryLruPolicy::on_request_end(const RequestContext& request, const RadixTree&) {
    if (retention_growth_seconds_per_block_ == 0 || request.affinity_id == 0) { return; }
    const auto blocks = request.ordered_hashes.size;
    const auto previous = sessions_.find(request.affinity_id);
    const auto growth = previous != sessions_.end() && blocks > previous->second.context_blocks
                            ? blocks - previous->second.context_blocks : 0;
    sessions_.insert_or_assign(request.affinity_id, SessionHistory{blocks, growth});
    // Publish this request's growth only after it finishes; evictions within it use prior history.
    for (NodeId id : request.ordered_hashes) {
        if (residents_.count(id)) { access_history_.at(id).growth_blocks = growth; }
    }
}

void ContextMemoryLruPolicy::on_commit(const MemoryMutation& mutation) {
    IndexedMemoryLruPolicy::on_commit(mutation);
    if (!adaptive_ || mutation.kind == MemoryMutationKind::Removed) { return; }
    auto [entry, inserted] = access_history_.try_emplace(mutation.node_id,
        AccessHistory{active_request_id_, active_timestamp_ns_, std::nullopt, active_growth_blocks_});
    auto& history = entry->second;
    if (!inserted && history.request_id != active_request_id_) {
        history.gap_ns = active_timestamp_ns_ - history.timestamp_ns;
        history.timestamp_ns = active_timestamp_ns_;
        history.request_id = active_request_id_;
        history.growth_blocks = active_growth_blocks_;
    }
}

void ContextMemoryLruPolicy::on_node_pruned(
    NodeId id, std::optional<NodeId> parent, const RadixTree& tree
) {
    IndexedMemoryLruPolicy::on_node_pruned(id, parent, tree);
    if (adaptive_) { access_history_.erase(id); }
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
