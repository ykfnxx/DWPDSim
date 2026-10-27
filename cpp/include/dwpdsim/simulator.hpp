#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <memory>
#include <optional>
#include <unordered_set>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/hybrid.hpp"
#include "dwpdsim/metrics.hpp"
#include "dwpdsim/policies/memory/memory_policy.hpp"
#include "dwpdsim/policies/storage/storage_policy.hpp"
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

    std::optional<HybridResult> process_hybrid_event(const HybridEvent& event);
    HybridPrefillResult process_hybrid_prefill(const HybridPrefillRequest& request,
        bool collect_results = true);
    const HybridRuntime& hybrid_stats() const;
    const HybridComponent* hybrid_component(NodeId id, std::size_t kind) const;

    void enable_memory_diagnostics(const std::string& path);
    void enable_request_diagnostics(const std::string& path);
    void finish();
    void finish(TimestampNs simulation_end_ns);

    const SimulationConfig& config() const noexcept;
    const MetricsCollector& metrics() const noexcept;
    const RadixTree& tree() const noexcept;
    const StorageState& storage() const noexcept;
    StoragePolicyStats storage_policy_stats() const;
    const ShadowFtl* shadow() const { return shadow_.get(); }
    StoragePolicyWork storage_policy_work() const { return config_.infinite_storage ? StoragePolicyWork{} : storage_policy_->work(); }
    MemoryPolicyWork memory_policy_work() const { return memory_policy_->work(); }
    std::uint64_t memory_decision_ns = 0;
    std::uint64_t memory_maintenance_ns = 0;
    std::uint64_t memory_decisions = 0;
    std::uint64_t trace_event_count() const noexcept;
    std::uint64_t memory_capacity_blocks() const noexcept;

  private:
    enum class ActionPlane : std::uint8_t {
        Capacity,
        Background,
    };

    void hybrid_require_enabled() const;
    void hybrid_validate_order(TimestampNs now, std::uint64_t sequence) const;
    void hybrid_create_path(const std::vector<HashId>& path, TimestampNs now);
    void hybrid_admit(NodeId id, std::size_t kind, TimestampNs now);
    void hybrid_update_pool_targets(std::uint64_t full_blocks, std::uint64_t tokens, TimestampNs now);
    void hybrid_write(NodeId id, std::size_t kind, TimestampNs now,
        std::optional<Placement> quoted_placement = std::nullopt);
    void hybrid_cost_evict(NodeId incoming, std::size_t kind,
        std::uint64_t required_bytes, TimestampNs now);
    std::uint64_t hybrid_io(NodeId id, std::size_t kind, const char* operation, TimestampNs now,
        TraceReason reason = TraceReason::StorageHit,
        std::optional<MoveId> move = std::nullopt,
        std::optional<std::uint64_t> dependency = std::nullopt,
        std::optional<StorageLocation> location = std::nullopt);
    bool hybrid_ensure_capacity(StorageTier tier, std::uint64_t units,
        const std::vector<NodeId>& protected_nodes, TimestampNs now);
    bool hybrid_execute_trim(const TrimIntent& intent, ActionPlane plane, TimestampNs now);
    bool hybrid_execute_relocation(const RelocateIntent& intent,
        const std::vector<NodeId>& protected_nodes, TimestampNs now);
    void hybrid_storage_accesses(const std::vector<std::pair<std::size_t, NodeId>>& reads,
        TimestampNs now);
    HybridComponent& hybrid_data(NodeId id, std::size_t kind);
    void hybrid_publish(NodeId id, std::size_t kind, std::uint64_t size, TimestampNs now);
    void hybrid_checkpoint_metadata(NodeId id, std::uint64_t tokens,
        std::optional<NodeId> previous, TimestampNs now, bool frontier = true);
    std::string hybrid_demand(NodeId id, std::size_t kind, TimestampNs now,
        std::vector<std::pair<std::size_t, NodeId>>& reads);

    static SimulationConfig validate_config(SimulationConfig config);

    StorageView storage_view() const;
    void collect_protected_prefix(
        const HashId* hash_ids,
        std::size_t hash_count,
        std::vector<NodeId>& protected_prefix
    ) const;
    void run_until(TimestampNs target_ns);
    void close_feedback_window(TimestampNs timestamp_ns);
    std::uint64_t emit_io(const TraceContext&, NodeId, Operation, const Node&,
        const StorageLocation&, TraceReason, std::optional<MoveId> = std::nullopt,
        std::optional<std::uint64_t> = std::nullopt);
    void drain_background_tick(TimestampNs timestamp_ns);
    AccessResult process_access(const AccessContext& context);
    void notify_memory_commit(const MemoryMutation& mutation);
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
    std::unique_ptr<HybridRuntime> hybrid_;
    StorageState storage_;
    std::unique_ptr<MemoryPolicy> memory_policy_;
    std::unique_ptr<StoragePolicy> storage_policy_;
    MetricsCollector metrics_;
    TraceWriter trace_writer_;
    std::ofstream memory_diagnostics_;
    std::ofstream request_diagnostics_;
    std::unique_ptr<ShadowFtl> shadow_;
    std::ofstream controller_windows_;
    TimestampNs feedback_period_ns_ = 0, next_feedback_ns_ = 0;
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
