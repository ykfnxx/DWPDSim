#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <optional>
#include <unordered_set>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/metrics.hpp"
#include "dwpdsim/policies/memory_policy.hpp"
#include "dwpdsim/policies/storage_policy.hpp"
#include "dwpdsim/radix_tree.hpp"
#include "dwpdsim/storage.hpp"
#include "dwpdsim/trace_writer.hpp"
#include "dwpdsim/types.hpp"

namespace dwpdsim {

class Simulator {
  public:
    Simulator(
        SimulationConfig config,
        std::unique_ptr<MemoryPolicy> memory_policy,
        std::unique_ptr<StoragePolicy> storage_policy,
        const std::filesystem::path& trace_path
    );

    void process_request(
        TimestampNs timestamp_ns,
        RequestId request_id,
        AffinityId affinity_id,
        const HashId* hash_ids,
        std::size_t hash_count
    );
    void process_request(
        TimestampNs timestamp_ns,
        RequestId request_id,
        AffinityId affinity_id,
        const std::vector<HashId>& hash_ids
    );

    void finish();
    void finish(TimestampNs simulation_end_ns);

    const SimulationConfig& config() const noexcept;
    const MetricsCollector& metrics() const noexcept;
    const RadixTree& tree() const noexcept;
    const StorageState& storage() const noexcept;
    StoragePolicyStats storage_policy_stats() const;
    std::uint64_t trace_event_count() const noexcept;
    std::uint64_t memory_capacity_blocks() const noexcept;

  private:
    enum class ActionPlane : std::uint8_t {
        Capacity,
        Background,
    };

    static SimulationConfig validate_config(SimulationConfig config);

    StorageView storage_view() const;
    void collect_protected_prefix(
        const HashId* hash_ids,
        std::size_t hash_count,
        std::vector<NodeId>& protected_prefix
    ) const;
    void run_until(TimestampNs target_ns);
    void drain_background_tick(TimestampNs timestamp_ns);
    void process_access(const AccessContext& context);
    void insert_into_memory(NodeId node_id, const AccessContext& context);
    void evict_from_memory(const AccessContext& context);
    bool dump_segment(
        const AccessContext& context,
        const SegmentView& segment,
        const std::vector<NodeId>& write_nodes
    );
    bool ensure_capacity(
        StorageTier tier,
        std::uint64_t required_blocks,
        CapacityCause cause,
        const std::vector<NodeId>& protected_nodes,
        const TraceContext& trace_context
    );
    bool execute_action(
        const StorageActionIntent& action,
        ActionPlane plane,
        const std::vector<NodeId>& protected_nodes,
        const TraceContext& trace_context
    );
    bool execute_trim(
        const TrimIntent& intent,
        ActionPlane plane,
        const TraceContext& trace_context
    );
    bool execute_relocation(
        const RelocateIntent& intent,
        const std::vector<NodeId>& protected_nodes,
        const TraceContext& trace_context,
        std::optional<NodeId> reused_read_node = std::nullopt,
        std::optional<std::uint64_t> reused_read_sequence = std::nullopt,
        bool capacity_ready = false,
        std::optional<MoveId> assigned_move_id = std::nullopt
    );
    void prune_segment(const std::vector<NodeId>& segment);
    void prune_from(NodeId node_id);
    void notify_storage_commit(const StorageMutation& mutation);

    SimulationConfig config_;
    std::uint64_t memory_capacity_blocks_;
    std::uint64_t memory_used_blocks_ = 0;
    RadixTree tree_;
    StorageState storage_;
    std::unique_ptr<MemoryPolicy> memory_policy_;
    std::unique_ptr<StoragePolicy> storage_policy_;
    MetricsCollector metrics_;
    TraceWriter trace_writer_;
    std::optional<TimestampNs> last_timestamp_ns_;
    std::optional<NodeId> active_node_id_;
    std::unordered_set<RequestId> request_ids_;
    std::uint64_t next_access_sequence_ = 0;
    MoveId next_move_id_ = 1;
    TimestampNs next_background_tick_ns_ = 0;
    std::vector<NodeId> protected_prefix_;
    std::vector<NodeId> memory_segment_scratch_;
    bool finished_ = false;
};

}  // namespace dwpdsim
