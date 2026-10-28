#include "dwpdsim/hybrid.hpp"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <tuple>
#include <unordered_set>
#include <utility>

namespace dwpdsim {

void HybridMemoryIndex::bind(const RadixTree& tree, const HybridAttentionConfig& config) {
    tree_ = &tree;
    use_lru_k_ = {config.full_policy == "lru_k", config.linear_policy == "lru_k"};
    lru_k_ = config.lru_k;
    frontier_ = config.linear_policy == "frontier_lru" || config.linear_policy == "frontier_guard";
    track_descendants_ = frontier_ || config.linear_retention_policy == "frontier";
    if (config.full_policy == "indexed_segment_lru" || config.full_policy == "context_lru") {
        full_ = std::make_unique<HybridFullLruIndex>();
        full_->bind_tree(tree);
    }
}

void HybridMemoryIndex::on_node_created(NodeId id, const RadixTree& tree) {
    for (auto& histories : reference_history_) { histories.erase(id); }
    if (full_) { full_->on_node_created(id, tree); }
}

void HybridMemoryIndex::begin_request() { ++request_clock_; }

HybridMemoryIndex::LruKKey HybridMemoryIndex::lru_k_key(
    std::size_t kind, NodeId id, std::uint64_t admission) const {
    const auto found = reference_history_[kind].find(id);
    if (found == reference_history_[kind].end()) { return {false, 0, 0, admission, id}; }
    const auto& history = found->second;
    const bool mature = history.requests.size() == lru_k_;
    const auto oldest = mature ? history.requests[history.next] : history.last_request;
    return {mature, oldest, history.last_request, admission, id};
}

void HybridMemoryIndex::reference(std::size_t kind, NodeId id) {
    if (!use_lru_k_[kind]) { return; }
    assert(request_clock_ != 0);
    const auto previous = reference_history_[kind].find(id);
    if (previous != reference_history_[kind].end() &&
        previous->second.last_request == request_clock_) { return; }
    const auto resident = keys_[kind].find(id);
    std::set<LruKKey>::node_type entry;
    if (resident != keys_[kind].end()) {
        entry = lru_k_order_[kind].extract(lru_k_key(kind, id, resident->second));
        assert(!entry.empty());
    }
    auto& history = reference_history_[kind][id];
    if (history.requests.size() < lru_k_) {
        history.requests.push_back(request_clock_);
    } else {
        history.requests[history.next] = request_clock_;
        history.next = (history.next + 1) % lru_k_;
    }
    history.last_request = request_clock_;
    if (resident != keys_[kind].end()) {
        entry.value() = lru_k_key(kind, id, resident->second);
        lru_k_order_[kind].insert(std::move(entry));
    }
}

void HybridMemoryIndex::touch(std::size_t kind, NodeId id) {
    if (use_lru_k_[kind]) {
        const auto [entry, inserted] = keys_[kind].try_emplace(id, recency_ + 1);
        if (inserted) {
            ++recency_;
            lru_k_order_[kind].insert(lru_k_key(kind, id, entry->second));
            if (kind == 1 && track_descendants_) {
                for (auto p = tree_->parent(id); p; p = tree_->parent(*p)) { ++linear_descendants_[*p]; }
            }
        }
        return;
    }
    auto previous = keys_[kind].find(id);
    const bool present = previous != keys_[kind].end();
    if (present) { lru_[kind].erase({previous->second, id}); }
    const auto key = ++recency_;
    keys_[kind][id] = key;
    lru_[kind].emplace(key, id);
    if (kind == 0 && full_) {
        full_->on_commit({present ? MemoryMutationKind::Accessed : MemoryMutationKind::Inserted, id});
    }
    if (kind == 1 && track_descendants_ && !present) {
        for (auto p = tree_->parent(id); p; p = tree_->parent(*p)) { ++linear_descendants_[*p]; }
    }
}

void HybridMemoryIndex::remove(std::size_t kind, NodeId id) {
    auto entry = keys_[kind].find(id);
    if (use_lru_k_[kind]) {
        lru_k_order_[kind].erase(lru_k_key(kind, id, entry->second));
    } else { lru_[kind].erase({entry->second, id}); }
    keys_[kind].erase(entry);
    if (kind == 0 && full_) { full_->on_commit({MemoryMutationKind::Removed, id}); }
    if (kind == 1 && track_descendants_) {
        for (auto p = tree_->parent(id); p; p = tree_->parent(*p)) {
            auto count = linear_descendants_.find(*p);
            assert(count != linear_descendants_.end() && count->second > 0);
            if (--count->second == 0) { linear_descendants_.erase(count); }
        }
    }
}

std::vector<std::vector<HybridCostObject>> HybridMemoryIndex::cost_candidates(
    const RadixTree& tree, std::uint64_t required, std::size_t groups) const {
    std::vector<std::vector<HybridCostObject>> result;
    for (std::size_t kind = 0; kind < 2; ++kind) {
        auto at = lru_k_order_[kind].begin();
        for (std::size_t group = 0; group < groups && at != lru_k_order_[kind].end(); ++group) {
            std::vector<HybridCostObject> objects;
            std::uint64_t bytes = 0;
            do {
                const auto id = std::get<4>(*at++);
                objects.push_back({id, kind});
                bytes += tree.node(id).hybrid->components[kind].size_bytes;
            } while (bytes < required && at != lru_k_order_[kind].end());
            result.push_back(std::move(objects));
        }
    }
    return result;
}

std::vector<NodeId> HybridMemoryIndex::victim(std::size_t kind, const RadixTree& tree,
    const HybridAttentionConfig& config, TimestampNs now, std::uint64_t& examined,
    std::optional<NodeId> incoming, bool drop_next, std::uint64_t candidate_budget) const {
    if (use_lru_k_[kind]) {
        ++examined;
        return {std::get<4>(*lru_k_order_[kind].begin())};
    }
    if (!candidate_budget) { candidate_budget = kind == 0 ? config.full_capacity_bytes : config.linear_capacity_bytes; }
    if (kind == 1) {
        if (frontier_) {
            if (config.linear_policy == "frontier_guard" && !drop_next) {
                ++examined;
                return {lru_[1].begin()->second};
            }
            std::unordered_set<NodeId> incoming_ancestors;
            if (incoming) {
                for (auto p = tree.parent(*incoming); p; p = tree.parent(*p)) {
                    if (keys_[1].count(*p)) { incoming_ancestors.insert(*p); }
                }
            }
            std::uint64_t covered = 0;
            for (const auto& key : lru_[1]) {
                ++examined;
                if (linear_descendants_.count(key.second) || incoming_ancestors.count(key.second)) {
                    return {key.second};
                }
                covered += tree.node(key.second).hybrid->components[1].size_bytes;
                if (static_cast<long double>(covered) >=
                    config.alpha * static_cast<long double>(candidate_budget)) { break; }
            }
            return {lru_[1].begin()->second};
        }
        if (config.linear_policy == "checkpoint_lru") {
            ++examined;
            return {lru_[kind].begin()->second};
        }
        long double best = std::numeric_limits<long double>::infinity();
        NodeId chosen = lru_[kind].begin()->second;
        std::uint64_t covered = 0;
        for (const auto& key : lru_[kind]) {
            ++examined;
            const auto& c = tree.node(key.second).hybrid->components[kind];
            const double age = static_cast<double>(now - c.last_demand_ns) / 1e9;
            long double score = c.frequency * std::exp(-age / config.decay_seconds);
            if (config.linear_policy == "benefit") {
                score *= config.linear_read_cost / static_cast<long double>(c.size_bytes);
            }
            if (config.linear_policy == "prefix_frequency" || config.linear_policy == "replay_value" ||
                config.linear_policy == "copy_aware" || config.linear_policy == "target_frequency" ||
                config.linear_policy == "target_value") {
                const double prefix_age = static_cast<double>(now - c.last_prefix_ns) / 1e9;
                score = c.prefix_frequency * std::exp(-prefix_age / config.decay_seconds);
                if (config.linear_policy == "target_frequency" || config.linear_policy == "target_value") {
                    const double target_age = static_cast<double>(now - c.last_target_ns) / 1e9;
                    score = c.target_frequency * std::exp(-target_age / config.decay_seconds);
                }
                if (config.linear_policy != "prefix_frequency" && config.linear_policy != "target_frequency") {
                    std::uint64_t fallback = 0;
                    auto previous = c.previous_checkpoint;
                    while (previous) {
                        const auto& ancestor = tree.node(*previous).hybrid->components[1];
                        if (ancestor.in_dram || ancestor.on_storage()) {
                            fallback = ancestor.checkpoint_tokens;
                            break;
                        }
                        previous = ancestor.previous_checkpoint;
                    }
                    const long double gap = static_cast<long double>(c.checkpoint_tokens - fallback) /
                        (config.checkpoint_generation == "request_end" ?
                            config.tokens_per_block : config.checkpoint_interval_tokens);
                    if (config.linear_policy == "replay_value") { score *= gap; }
                    else {
                        score *= config.linear_read_cost + (!c.on_storage() ?
                            config.recompute_weight * gap : 0);
                    }
                    score /= c.size_bytes;
                }
            }
            // Strict comparison preserves LRU order on ties.
            if (score < best) { best = score; chosen = key.second; }
            covered += c.size_bytes;
            if (static_cast<long double>(covered) >=
                config.alpha * static_cast<long double>(candidate_budget)) { break; }
        }
        return {chosen};
    }

    if (full_) {
        const auto before = full_->work().candidates_examined;
        const auto context_blocks = config.full_policy == "context_lru" ?
            config.alpha * static_cast<double>(candidate_budget / config.full_block_bytes) : 0;
        const auto chosen = full_->victim(context_blocks);
        examined += full_->work().candidates_examined - before;
        std::vector<NodeId> path, result;
        tree.resolve_segment(chosen, path);
        for (auto i = path.rbegin(); i != path.rend(); ++i) {
            const auto& node = tree.node(*i);
            if (node.hybrid && node.hybrid->components[0].in_dram) { result.push_back(*i); }
        }
        return result;
    }

    // FULL keeps segment LRU and the resident-descendant restriction. The scan
    // reads component state, not legacy Node::in_memory or LINEAR residency.
    std::unordered_map<NodeId, std::uint64_t> segments;
    std::unordered_map<NodeId, std::size_t> descendants;
    for (const auto& key : lru_[0]) {
        ++examined;
        auto& s = segments[tree.segment_leaf_for(key.second)];
        s = std::max(s, key.first);
        for (auto p = tree.parent(key.second); p; p = tree.parent(*p)) { ++descendants[*p]; }
    }
    std::optional<std::tuple<std::uint64_t, NodeId>> best;
    NodeId chosen = 0;
    for (const auto& item : segments) {
        if (descendants[item.first] != 0) { continue; }
        const auto score = std::make_tuple(item.second, item.first);
        if (!best || score < *best) { best = score; chosen = item.first; }
    }
    if (!best) { throw std::logic_error("FULL pool has no legal victim"); }
    std::vector<NodeId> path, result;
    tree.resolve_segment(chosen, path);
    for (auto i = path.rbegin(); i != path.rend(); ++i) {
        const auto& node = tree.node(*i);
        if (node.hybrid && node.hybrid->components[0].in_dram) { result.push_back(*i); }
    }
    return result;
}

HybridEvictionDecision HybridMemoryIndex::decide(std::size_t kind, const RadixTree& tree,
    const HybridAttentionConfig& config, TimestampNs now, std::uint64_t& examined,
    std::optional<NodeId> incoming, bool legacy_drop, std::uint64_t candidate_budget) const {
    HybridEvictionDecision result{victim(kind, tree, config, now, examined, incoming,
        legacy_drop, candidate_budget), legacy_drop, kind};
    if (config.eviction_mode != "retention") { return result; }
    TimestampNs last_touch = 0;
    for (const auto id : result.victims) {
        last_touch = std::max(last_touch, tree.node(id).hybrid->components[kind].last_touch_ns);
    }
    const auto retention = kind == 0 ? config.full_retention_ns : config.linear_retention_ns;
    result.drop = retention && now - last_touch > *retention;
    if (kind == 1 && config.linear_retention_policy == "frontier") {
        const auto id = result.victims.front();
        bool covered = linear_descendants_.count(id) != 0;
        // incoming is supplied only for a fully produced LINEAR state, never for FULL.
        for (auto p = incoming ? tree.parent(*incoming) : std::nullopt; !covered && p; p = tree.parent(*p)) {
            covered = *p == id;
        }
        result.drop = result.drop || covered;
    }
    return result;
}

HybridEvictionDecision HybridMemoryIndex::decide_shared(const RadixTree& tree,
    const HybridAttentionConfig& config, TimestampNs now,
    std::array<HybridCounters, 2>& counters, const std::array<bool, 2>& legacy_drop,
    std::size_t incoming_kind, NodeId incoming, std::uint64_t capacity) const {
    const auto candidate = [&](std::size_t kind) {
        return decide(kind, tree, config, now, counters[kind].candidates_examined,
            kind == incoming_kind ? std::optional<NodeId>(incoming) : std::nullopt,
            legacy_drop[kind], capacity);
    };
    assert(!keys_[0].empty() || !keys_[1].empty());
    if (keys_[0].empty()) { return candidate(1); }
    if (keys_[1].empty()) { return candidate(0); }
    if (use_lru_k_[0] && use_lru_k_[1]) {
        // Both sets use the same request and admission clocks. Comparing their
        // heads is equivalent to one global LRU-K order over typed objects.
        return candidate(*lru_k_order_[0].begin() < *lru_k_order_[1].begin() ? 0 : 1);
    }
    auto full = candidate(0);
    auto linear = candidate(1);
    // Segment LRU ranks a FULL segment by its most recent resident object.
    // LINEAR checkpoint LRU has one object per candidate on the same clock.
    const auto recency = [&](const HybridEvictionDecision& choice) {
        std::uint64_t latest = 0;
        for (const auto id : choice.victims) { latest = std::max(latest, keys_[choice.kind].at(id)); }
        return latest;
    };
    return recency(full) < recency(linear) ? full : linear;
}

} // namespace dwpdsim
