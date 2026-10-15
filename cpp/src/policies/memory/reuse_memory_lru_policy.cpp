#include "dwpdsim/policies/memory/reuse_memory_lru_policy.hpp"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <stdexcept>
#include <tuple>

#include "dwpdsim/radix_tree.hpp"

namespace dwpdsim {

ReuseMemoryLruPolicy::ReuseMemoryLruPolicy(bool admit, std::uint64_t capacity, double alpha,
    std::optional<TimestampNs> retention, std::optional<std::uint64_t> max_blocks,
    std::vector<double> edges, std::vector<std::vector<double>> curves,
    std::optional<double> dump_probability, bool priority,
    std::optional<double> admit_probability, double short_horizon, double long_horizon,
    std::vector<std::vector<std::vector<double>>> prefix_keep,
    bool cost_weighted, double recompute_weight)
    : IndexedMemoryLruPolicy(admit, 1, 0, 1, 0, retention), budget_(alpha * capacity),
      max_blocks_(max_blocks), retention_for_cost_(retention),
      edges_(std::move(edges)), survival_(std::move(curves)),
      prefix_keep_(std::move(prefix_keep)), cost_weighted_(cost_weighted),
      recompute_weight_(recompute_weight),
      dump_probability_(dump_probability), admit_probability_(admit_probability),
      priority_(priority), admit_(admit), short_horizon_(short_horizon),
      long_horizon_(long_horizon) {
    work_.evicted_idle_bins.resize(8);
    work_.evicted_residency_bins.resize(8);
    if (!std::isfinite(recompute_weight_) || recompute_weight_ <= 0 ||
        ((!prefix_keep_.empty() || cost_weighted_) && !priority_)) {
        throw std::invalid_argument("reuse prefix/cost scoring requires priority and positive cost weight");
    }
    if (!(alpha > 0 && alpha <= 1) || max_blocks == 0 ||
        !std::isfinite(short_horizon) || short_horizon <= 0 ||
        !std::isfinite(long_horizon) || long_horizon <= 0) {
        throw std::invalid_argument("invalid reuse_lru budget, grain or horizon");
    }
    for (auto p : {dump_probability, admit_probability}) {
        if (p && (!std::isfinite(*p) || *p < 0 || *p > 1)) {
            throw std::invalid_argument("reuse probability must be in [0, 1]");
        }
    }
    if (edges_.size() < 2 || edges_.front() != 0 || survival_.size() != 9) {
        throw std::invalid_argument("reuse_lru requires edges starting at zero and nine survival rows");
    }
    for (std::size_t i = 0; i < edges_.size(); ++i) {
        if (!std::isfinite(edges_[i]) || (i && edges_[i] <= edges_[i - 1])) {
            throw std::invalid_argument("reuse edges must be finite and strictly increasing");
        }
    }
    for (const auto& row : survival_) {
        if (row.size() != edges_.size() || row.front() != 1) {
            throw std::invalid_argument("reuse survival shape mismatch or S(0) != 1");
        }
        for (std::size_t i = 0; i < row.size(); ++i) {
            if (!std::isfinite(row[i]) || row[i] < 0 || row[i] > 1 ||
                (i && row[i] > row[i - 1])) {
                throw std::invalid_argument("reuse survival must be nonincreasing in [0, 1]");
            }
        }
    }
    if (!prefix_keep_.empty()) {
        if (prefix_keep_.size() != 9) {
            throw std::invalid_argument("reuse prefix model requires nine groups");
        }
        for (const auto& group : prefix_keep_) {
            if (group.size() + 1 != edges_.size()) {
                throw std::invalid_argument("reuse prefix model requires one row per gap interval");
            }
            for (const auto& row : group) {
                if (row.size() != 3) {
                    throw std::invalid_argument("reuse prefix model requires three depth bins");
                }
                for (double p : row) {
                    if (!std::isfinite(p) || p < 0 || p > 1) {
                        throw std::invalid_argument("reuse prefix probabilities must be in [0, 1]");
                    }
                }
            }
        }
    }
}

void ReuseMemoryLruPolicy::on_request_begin(const RequestContext& r, const RadixTree&) {
    now_ = r.timestamp_ns;
    std::size_t state = 0;
    if (r.affinity_id != 0) {
        const auto previous = sessions_.find(r.affinity_id);
        if (previous != sessions_.end()) {
            state = now_ - previous->second <= 60000000000ULL ? 1 : 2;
        }
        sessions_.insert_or_assign(r.affinity_id, now_);
    }
    const auto n = r.ordered_hashes.size;
    active_group_ = state * 3 + (n <= 128 ? 0 : n <= 512 ? 1 : 2);
    active_context_ = n;
    if (!prefix_keep_.empty() || cost_weighted_) {
        active_positions_.clear();
        for (std::size_t i = 0; i < n; ++i) {
            active_positions_.insert_or_assign(r.ordered_hashes[i], i);
        }
    }
}

void ReuseMemoryLruPolicy::on_commit(const MemoryMutation& m) {
    IndexedMemoryLruPolicy::on_commit(m);
    if (m.kind != MemoryMutationKind::Removed) {
        groups_by_node_.insert_or_assign(m.node_id, active_group_);
        const auto admitted = m.kind == MemoryMutationKind::Inserted
                                  ? now_ : features_.at(m.node_id).admitted_at;
        features_.insert_or_assign(m.node_id, Feature{active_group_, active_context_,
            (!prefix_keep_.empty() || cost_weighted_) ? active_positions_.at(m.node_id) : 0,
            admitted});
    } else {
        groups_by_node_.erase(m.node_id);
        const double bounds[] = {1, 4, 16, 60, 300, 1800, 7200};
        const double idle = static_cast<double>(now_ - tree_->node(m.node_id).last_access_timestamp_ns) / 1e9;
        const double residence = static_cast<double>(now_ - features_.at(m.node_id).admitted_at) / 1e9;
        ++work_.evicted_idle_bins[std::upper_bound(std::begin(bounds), std::end(bounds), idle) - std::begin(bounds)];
        ++work_.evicted_residency_bins[std::upper_bound(std::begin(bounds), std::end(bounds), residence) - std::begin(bounds)];
        features_.erase(m.node_id);
    }
}

void ReuseMemoryLruPolicy::on_node_pruned(NodeId id, std::optional<NodeId> parent,
                                       const RadixTree& tree) {
    IndexedMemoryLruPolicy::on_node_pruned(id, parent, tree);
    groups_by_node_.erase(id);
    features_.erase(id);
}

double ReuseMemoryLruPolicy::survival(std::size_t group, double age) const {
    const auto& row = survival_[group];
    auto it = std::upper_bound(edges_.begin(), edges_.end(), age);
    if (it == edges_.end()) { return row.back(); }
    const auto hi = static_cast<std::size_t>(it - edges_.begin());
    const auto lo = hi - 1;
    const double f = (age - edges_[lo]) / (edges_[hi] - edges_[lo]);
    return row[lo] + f * (row[hi] - row[lo]);
}

double ReuseMemoryLruPolicy::probability(std::size_t group, double age, double horizon) const {
    const double s = survival(group, age);
    return s > 0 ? std::clamp(1 - survival(group, age + horizon) / s, 0.0, 1.0) : 0;
}

double ReuseMemoryLruPolicy::segment_probability(NodeId endpoint, double horizon) const {
    const auto& segment = segments_.at(endpoints_.at(endpoint));
    const auto newest = segment.members.rbegin()->second;
    const double age = static_cast<double>(now_ - tree_->node(newest).last_access_timestamp_ns) / 1e9;
    return probability(groups_by_node_.at(newest), age, horizon);
}

NodeId ReuseMemoryLruPolicy::select_victim() const {
    if (!priority_) { return IndexedMemoryLruPolicy::select_victim(); }
    using Score = std::tuple<double, Key>;
    std::optional<Score> best;
    std::size_t covered = 0;
    for (const Key& key : groups_.front()) {
        ++work_.candidates_examined;
        const auto endpoint = key.second;
        const auto it = subtree_memory_.find(endpoint);
        if ((it == subtree_memory_.end() ? 0 : it->second) != residents_.count(endpoint)) { continue; }
        const Score score{(!prefix_keep_.empty() || cost_weighted_)
                              ? segment_value(endpoint)
                              : segment_probability(endpoint, short_horizon_), key};
        if (!best || score < *best) { best = score; }
        covered += segments_.at(endpoints_.at(endpoint)).members.size();
        if (covered >= budget_) { break; }
    }
    assert(best);
    return std::get<1>(*best).second;
}

double ReuseMemoryLruPolicy::segment_value(NodeId endpoint) const {
    // Score exactly the resident tail that the simulator would evict. Capacity
    // pressure determines the marginal value; no hard TTL pins resident blocks.
    tree_->resolve_segment(endpoint, value_scratch_);
    double value = 0;
    std::size_t count = 0;
    const auto newest = segments_.at(endpoints_.at(endpoint)).members.rbegin()->second;
    const bool would_drop = dump_probability_
        ? segment_probability(endpoint, long_horizon_) < *dump_probability_
        : retention_for_cost_ && now_ - tree_->node(newest).last_access_timestamp_ns > *retention_for_cost_;
    for (auto it = value_scratch_.rbegin(); it != value_scratch_.rend(); ++it) {
        const auto& node = tree_->node(*it);
        if (!node.in_memory) { continue; }
        const auto& f = features_.at(*it);
        const double age = static_cast<double>(now_ - node.last_access_timestamp_ns) / 1e9;
        double p = probability(f.group, age, short_horizon_);
        if (!prefix_keep_.empty()) {
            const auto depth_bin = std::min<std::size_t>(2, 3 * f.position / f.context);
            double mass = 0;
            const double s = survival(f.group, age);
            for (std::size_t k = 0; k + 1 < edges_.size(); ++k) {
                const double lo = std::max(age, edges_[k]);
                const double hi = std::min(age + short_horizon_, edges_[k + 1]);
                if (hi > lo) {
                    mass += (survival(f.group, lo) - survival(f.group, hi)) *
                            prefix_keep_[f.group][k][depth_bin];
                }
            }
            p = s > 0 ? std::clamp(mass / s, 0.0, 1.0) : 0;
        }
        // READ = one proxy unit/block; missing-copy recompute = context blocks.
        // This is an explicit proxy exchange rate, not measured latency.
        const double cost = !cost_weighted_ || node.on_storage || !would_drop
                                ? 1.0 : recompute_weight_ * f.context;
        value += p * cost;
        if (++count == max_blocks_.value_or(UINT64_MAX)) { break; }
    }
    assert(count > 0);
    // All blocks have equal byte size, so omit that common scale factor.
    return value / (count * short_horizon_);
}

MemoryEvictionDecision ReuseMemoryLruPolicy::evict(const RequestContext& r,
                                                const RadixTree& tree) const {
    auto d = IndexedMemoryLruPolicy::evict(r, tree);
    d.reclaim_parent = false;
    d.max_eviction_blocks = max_blocks_;
    if (dump_probability_) {
        d.action = segment_probability(d.leaf_segment_endpoint, long_horizon_) >= *dump_probability_
                       ? MemoryEvictionAction::Dump : MemoryEvictionAction::Drop;
    }
    return d;
}

bool ReuseMemoryLruPolicy::admit_storage_hit(const AccessContext&, const Node&,
                                          const RadixTree&) const {
    return admit_ && (!admit_probability_ ||
        probability(active_group_, 0, short_horizon_) >= *admit_probability_);
}

}  // namespace dwpdsim
