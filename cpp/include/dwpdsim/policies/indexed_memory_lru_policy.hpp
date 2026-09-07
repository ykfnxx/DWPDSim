#pragma once

#include <condition_variable>
#include <mutex>
#include <set>
#include <thread>
#include <unordered_map>
#include <vector>

#include "dwpdsim/policies/memory_policy.hpp"

namespace dwpdsim {

// Exact segment recency, with independently selectable grouping, sampling and workers.
// Workers read only during evict(); all mutations remain on the simulator thread.
class IndexedMemoryLruPolicy final : public MemoryPolicy {
  public:
    IndexedMemoryLruPolicy(bool admit_storage_hits, std::size_t groups,
                           std::size_t sampled_groups, std::size_t workers, std::uint64_t seed,
                           std::optional<TimestampNs> retention_ns = std::nullopt);
    ~IndexedMemoryLruPolicy() override;
    void bind_tree(const RadixTree& tree) override;
    bool admit_storage_hit(const AccessContext&, const Node&, const RadixTree&) const override;
    MemoryEvictionDecision evict(const RequestContext&, const RadixTree&) const override;
    void on_commit(const MemoryMutation&) override;
    void on_node_created(NodeId node_id, const RadixTree& tree) override;
    void on_node_pruned(NodeId node_id, std::optional<NodeId> parent,
                        const RadixTree& tree) override;
    MemoryPolicyWork work() const override;

  private:
    using Key = std::pair<std::uint64_t, NodeId>;
    using Handle = std::uint64_t;
    struct Resident { std::uint64_t recency; Handle segment; };
    struct Segment {
        NodeId endpoint;
        std::size_t group;
        std::set<Key> members;
    };
    struct Result { std::optional<Key> best; std::uint64_t examined = 0; };
    Handle ensure_segment(NodeId endpoint);
    void unpublish(Handle handle);
    void publish(Handle handle);
    void move_endpoint(Handle handle, NodeId endpoint);
    void merge(NodeId prefix, NodeId suffix);
    void change_ancestors(NodeId node_id, bool insert);
    Result search(std::size_t worker, std::size_t workers) const;
    Result search_groups() const;
    void worker_loop(std::size_t worker);
    static std::uint64_t mix(std::uint64_t value);

    bool admit_storage_hits_;
    std::size_t sampled_groups_;
    std::uint64_t seed_;
    std::optional<TimestampNs> retention_ns_;
    const RadixTree* tree_ = nullptr;
    std::uint64_t next_recency_ = 0;
    Handle next_handle_ = 0;
    std::unordered_map<NodeId, Resident> residents_;
    std::unordered_map<NodeId, std::uint64_t> subtree_memory_;
    std::unordered_map<NodeId, Handle> endpoints_;
    std::unordered_map<Handle, Segment> segments_;
    std::vector<std::set<Key>> groups_;
    std::vector<NodeId> scratch_;
    mutable MemoryPolicyWork work_;
    mutable std::uint64_t decision_ = 0;
    mutable std::vector<std::size_t> selected_groups_;
    std::vector<std::thread> threads_;
    mutable std::vector<Result> results_;
    mutable std::mutex mutex_;
    mutable std::condition_variable ready_, done_;
    mutable std::uint64_t epoch_ = 0;
    mutable std::size_t pending_ = 0;
    bool stopping_ = false;
};

}  // namespace dwpdsim
