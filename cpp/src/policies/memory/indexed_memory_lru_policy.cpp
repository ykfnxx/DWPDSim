#include "dwpdsim/policies/memory/indexed_memory_lru_policy.hpp"

#include <algorithm>
#include <cassert>
#include <stdexcept>

#include "dwpdsim/radix_tree.hpp"

namespace dwpdsim {

IndexedMemoryLruPolicy::IndexedMemoryLruPolicy(
    bool admit_storage_hits, std::size_t groups, std::size_t sampled_groups,
    std::size_t workers, std::uint64_t seed, std::optional<TimestampNs> retention_ns
) : admit_storage_hits_(admit_storage_hits), sampled_groups_(sampled_groups), seed_(seed),
    retention_ns_(retention_ns) {
    if (groups == 0 || workers == 0 || workers > groups || sampled_groups > groups) {
        throw std::invalid_argument("memory groups/workers must be positive, workers and sample <= groups");
    }
    groups_.resize(groups);
    if (workers > 1) {
        results_.resize(workers);
        try {
            for (std::size_t i = 0; i < workers; ++i) {
                threads_.emplace_back(&IndexedMemoryLruPolicy::worker_loop, this, i);
            }
        } catch (...) {
            { std::lock_guard<std::mutex> lock(mutex_); stopping_ = true; }
            ready_.notify_all();
            for (auto& thread : threads_) { thread.join(); }
            throw;
        }
    }
}

IndexedMemoryLruPolicy::~IndexedMemoryLruPolicy() {
    { std::lock_guard<std::mutex> lock(mutex_); stopping_ = true; }
    ready_.notify_all();
    for (auto& thread : threads_) { thread.join(); }
}

void IndexedMemoryLruPolicy::bind_tree(const RadixTree& tree) { tree_ = &tree; }

bool IndexedMemoryLruPolicy::admit_storage_hit(
    const AccessContext&, const Node&, const RadixTree&
) const { return admit_storage_hits_; }

std::uint64_t IndexedMemoryLruPolicy::mix(std::uint64_t value) {
    value += 0x9e3779b97f4a7c15ULL;
    value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31);
}

IndexedMemoryLruPolicy::Handle IndexedMemoryLruPolicy::ensure_segment(NodeId endpoint) {
    const auto found = endpoints_.find(endpoint);
    if (found != endpoints_.end()) { return found->second; }
    const Handle handle = ++next_handle_;
    segments_.emplace(handle, Segment{endpoint, mix(endpoint) % groups_.size(), {}});
    endpoints_.emplace(endpoint, handle);
    return handle;
}

void IndexedMemoryLruPolicy::unpublish(Handle handle) {
    const auto& segment = segments_.at(handle);
    if (!segment.members.empty()) {
        groups_[segment.group].erase({segment.members.rbegin()->first, segment.endpoint});
    }
}

void IndexedMemoryLruPolicy::publish(Handle handle) {
    const auto& segment = segments_.at(handle);
    if (segment.members.empty()) {
        endpoints_.erase(segment.endpoint);
        segments_.erase(handle);
    } else {
        groups_[segment.group].emplace(segment.members.rbegin()->first, segment.endpoint);
    }
}

void IndexedMemoryLruPolicy::move_endpoint(Handle handle, NodeId endpoint) {
    unpublish(handle);
    auto& segment = segments_.at(handle);
    endpoints_.erase(segment.endpoint);
    segment.endpoint = endpoint;
    endpoints_.emplace(endpoint, handle);
    publish(handle);
}

void IndexedMemoryLruPolicy::merge(NodeId prefix, NodeId suffix) {
    const auto above = endpoints_.find(prefix);
    if (above == endpoints_.end()) { return; }
    const auto below = endpoints_.find(suffix);
    if (below == endpoints_.end()) { move_endpoint(above->second, suffix); return; }
    const Handle a = above->second, b = below->second;
    unpublish(a);
    unpublish(b);
    for (const auto& key : segments_.at(a).members) {
        residents_.at(key.second).segment = b;
        ++work_.topology_member_moves;
    }
    segments_.at(b).members.merge(segments_.at(a).members);
    endpoints_.erase(prefix);
    segments_.erase(a);
    publish(b);
}

void IndexedMemoryLruPolicy::on_node_created(NodeId node_id, const RadixTree& tree) {
    const auto parent = tree.parent(node_id);
    if (!parent) { return; }
    if (tree.child_count(*parent) == 1) {
        const auto old = endpoints_.find(*parent);
        if (old != endpoints_.end()) { move_endpoint(old->second, node_id); }
    } else if (tree.child_count(*parent) == 2) {
        // The old endpoint remains on the original suffix. Only prefix residents move.
        tree.resolve_segment(*parent, scratch_);
        std::optional<Handle> prefix, suffix;
        for (NodeId id : scratch_) {
            ++work_.topology_nodes_examined;
            auto member = residents_.find(id);
            if (member == residents_.end()) { continue; }
            if (!prefix) {
                suffix = member->second.segment;
                unpublish(*suffix);
                prefix = ensure_segment(*parent);
            }
            const Key key{member->second.recency, id};
            segments_.at(*suffix).members.erase(key);
            segments_.at(*prefix).members.insert(key);
            member->second.segment = *prefix;
            ++work_.topology_member_moves;
        }
        if (prefix) { publish(*suffix); publish(*prefix); }
    }
}

void IndexedMemoryLruPolicy::on_node_pruned(
    NodeId node_id, std::optional<NodeId> parent, const RadixTree& tree
) {
    assert(residents_.find(node_id) == residents_.end());
    if (!parent) { return; }
    if (tree.child_count(*parent) == 0) {
        const auto old = endpoints_.find(node_id);
        if (old != endpoints_.end()) { move_endpoint(old->second, *parent); }
    } else if (tree.child_count(*parent) == 1) {
        merge(*parent, tree.segment_leaf_for(*parent));
    }
}

void IndexedMemoryLruPolicy::change_ancestors(NodeId node_id, bool insert) {
    std::optional<NodeId> current = node_id;
    while (current) {
        ++work_.ancestor_updates;
        if (insert) { ++subtree_memory_[*current]; }
        else {
            auto entry = subtree_memory_.find(*current);
            assert(entry != subtree_memory_.end() && entry->second > 0);
            if (--entry->second == 0) { subtree_memory_.erase(entry); }
        }
        current = tree_->parent(*current);
    }
}

void IndexedMemoryLruPolicy::on_commit(const MemoryMutation& mutation) {
    const NodeId id = mutation.node_id;
    if (mutation.kind == MemoryMutationKind::Inserted) {
        const Handle handle = ensure_segment(tree_->segment_leaf_for(id));
        unpublish(handle);
        const auto sequence = ++next_recency_;
        residents_.emplace(id, Resident{sequence, handle});
        segments_.at(handle).members.emplace(sequence, id);
        change_ancestors(id, true);
        publish(handle);
    } else {
        auto entry = residents_.find(id);
        const Handle handle = entry->second.segment;
        unpublish(handle);
        auto& members = segments_.at(handle).members;
        members.erase({entry->second.recency, id});
        if (mutation.kind == MemoryMutationKind::Accessed) {
            entry->second.recency = ++next_recency_;
            members.emplace(entry->second.recency, id);
        } else {
            change_ancestors(id, false);
            residents_.erase(entry);
        }
        publish(handle);
    }
}

IndexedMemoryLruPolicy::Result IndexedMemoryLruPolicy::search(
    std::size_t worker, std::size_t workers
) const {
    Result result;
    for (std::size_t i = worker; i < selected_groups_.size(); i += workers) {
        for (const Key& key : groups_[selected_groups_[i]]) {
            ++result.examined;
            const auto count = subtree_memory_.find(key.second);
            const std::uint64_t subtree = count == subtree_memory_.end() ? 0 : count->second;
            const std::uint64_t self = residents_.count(key.second);
            if (subtree == self) {
                if (!result.best || key < *result.best) { result.best = key; }
                break;
            }
        }
    }
    return result;
}

void IndexedMemoryLruPolicy::worker_loop(std::size_t worker) {
    std::uint64_t seen = 0;
    std::unique_lock<std::mutex> lock(mutex_);
    while (true) {
        ready_.wait(lock, [&] { return stopping_ || epoch_ != seen; });
        if (stopping_) { return; }
        seen = epoch_;
        lock.unlock();
        const auto result = search(worker, results_.size());
        lock.lock();
        results_[worker] = result;
        if (--pending_ == 0) { done_.notify_one(); }
    }
}

IndexedMemoryLruPolicy::Result IndexedMemoryLruPolicy::search_groups() const {
    if (threads_.empty()) { return search(0, 1); }
    std::unique_lock<std::mutex> lock(mutex_);
    pending_ = threads_.size();
    ++epoch_;
    ++work_.worker_rounds;
    ready_.notify_all();
    done_.wait(lock, [&] { return pending_ == 0; });
    Result result;
    for (const auto& part : results_) {
        result.examined += part.examined;
        if (part.best && (!result.best || *part.best < *result.best)) { result.best = part.best; }
    }
    return result;
}

MemoryEvictionDecision IndexedMemoryLruPolicy::evict(
    const RequestContext& request, const RadixTree& tree
) const {
    const std::size_t count = sampled_groups_ == 0 ? groups_.size() : sampled_groups_;
    const std::size_t start = mix(seed_ ^ decision_++) % groups_.size();
    selected_groups_.clear();
    for (std::size_t i = 0; i < count; ++i) {
        selected_groups_.push_back((start + i) % groups_.size());
    }
    auto result = search_groups();
    work_.candidates_examined += result.examined;
    if (!result.best && count < groups_.size()) {
        selected_groups_.clear();
        for (std::size_t i = count; i < groups_.size(); ++i) {
            selected_groups_.push_back((start + i) % groups_.size());
        }
        result = search_groups();
        work_.candidates_examined += result.examined;
    }
    assert(result.best.has_value());
    const NodeId endpoint = result.best->second;
    auto action = MemoryEvictionAction::Dump;
    if (retention_ns_) {
        const auto& segment = segments_.at(endpoints_.at(endpoint));
        // Memory recency order also orders timestamps on the nondecreasing timeline.
        // Read the newest resident after workers finish; no segment scan is needed.
        const NodeId newest = segment.members.rbegin()->second;
        const TimestampNs last_access = tree.node(newest).last_access_timestamp_ns;
        assert(request.timestamp_ns >= last_access);
        if (request.timestamp_ns - last_access > *retention_ns_) {
            action = MemoryEvictionAction::Drop;
        }
    }
    return {endpoint, action};
}

MemoryPolicyWork IndexedMemoryLruPolicy::work() const {
    auto result = work_;
    result.indexed_segments = segments_.size();
    result.indexed_residents = residents_.size();
    result.ancestor_entries = subtree_memory_.size();
    return result;
}

}  // namespace dwpdsim
