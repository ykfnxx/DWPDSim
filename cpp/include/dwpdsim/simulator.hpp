#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <optional>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/metrics.hpp"
#include "dwpdsim/policies.hpp"
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
        std::unique_ptr<WritePlacementPolicy> placement_policy,
        std::unique_ptr<StorageEvictionPolicy> storage_eviction_policy,
        const std::filesystem::path& trace_path
    );

    void process_request(
        Timestamp timestamp,
        const HashId* hash_ids,
        std::size_t hash_count
    );
    void process_request(Timestamp timestamp, const std::vector<HashId>& hash_ids);

    void finish();

    const SimulationConfig& config() const noexcept;
    const MetricsCollector& metrics() const noexcept;
    const RadixTree& tree() const noexcept;
    const StorageState& storage() const noexcept;
    std::uint64_t trace_event_count() const noexcept;
    std::uint64_t memory_capacity_blocks() const noexcept;

  private:
    static SimulationConfig validate_config(SimulationConfig config);

    void process_access(const AccessContext& context);
    void insert_into_memory(NodeId node_id, const AccessContext& context);
    void evict_from_memory(const AccessContext& context);
    void write_to_storage(NodeId node_id, const AccessContext& context);
    void trim_from_storage(NodeId node_id, const AccessContext& context);

    SimulationConfig config_;
    std::uint64_t memory_capacity_blocks_;
    std::uint64_t memory_used_blocks_ = 0;
    RadixTree tree_;
    StorageState storage_;
    std::unique_ptr<MemoryPolicy> memory_policy_;
    std::unique_ptr<WritePlacementPolicy> placement_policy_;
    std::unique_ptr<StorageEvictionPolicy> storage_eviction_policy_;
    MetricsCollector metrics_;
    TraceWriter trace_writer_;
    std::optional<Timestamp> last_timestamp_;
    std::uint64_t next_access_sequence_ = 0;
};

}  // namespace dwpdsim
