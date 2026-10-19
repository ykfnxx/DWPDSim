#include "dwpdsim/hybrid.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <tuple>

namespace dwpdsim {

void HybridMemoryIndex::touch(std::size_t kind, NodeId id) {
    remove(kind, id);
    const auto key = ++recency_;
    keys_[kind].emplace(id, key);
    lru_[kind].emplace(key, id);
}

void HybridMemoryIndex::remove(std::size_t kind, NodeId id) {
    auto entry = keys_[kind].find(id);
    if (entry == keys_[kind].end()) { return; }
    lru_[kind].erase({entry->second, id});
    keys_[kind].erase(entry);
}

std::vector<NodeId> HybridMemoryIndex::victim(std::size_t kind, const RadixTree& tree,
    const HybridAttentionConfig& config, TimestampNs now, std::uint64_t& examined) const {
    if (kind == 1) {
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
            // Strict comparison preserves LRU order on ties.
            if (score < best) { best = score; chosen = key.second; }
            covered += c.size_bytes;
            if (static_cast<long double>(covered) >=
                config.alpha * static_cast<long double>(config.linear_capacity_bytes)) { break; }
        }
        return {chosen};
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

} // namespace dwpdsim
