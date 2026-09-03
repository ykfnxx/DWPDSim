#include <cassert>
#include <filesystem>
#include <fstream>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/policies.hpp"
#include "dwpdsim/simulator.hpp"
#include "dwpdsim/types.hpp"

namespace {

class RecordingMemoryPolicy final : public dwpdsim::MemoryPolicy {
  public:
    bool admit_storage_hit(
        const dwpdsim::AccessContext&,
        const dwpdsim::Node&,
        const dwpdsim::RadixTree&
    ) override {
        return true;
    }

    dwpdsim::NodeId choose_victim(
        const dwpdsim::AccessContext&,
        const dwpdsim::RadixTree& tree
    ) override {
        return tree.segment_leaf_for(resident_.front());
    }

    dwpdsim::EvictionAction eviction_action(
        const dwpdsim::Node& victim,
        const dwpdsim::AccessContext&,
        const dwpdsim::RadixTree& tree
    ) override {
        decision_access_counts.push_back(victim.access_count);
        decision_tree_visible = &victim == &tree.node(victim.hash_id);
        return dwpdsim::EvictionAction::Drop;
    }

    void on_memory_insert(dwpdsim::NodeId node_id) override {
        resident_.push_back(node_id);
    }

    void on_memory_access(dwpdsim::NodeId) override {}

    void on_memory_remove(dwpdsim::NodeId node_id) override {
        for (auto it = resident_.begin(); it != resident_.end(); ++it) {
            if (*it == node_id) {
                resident_.erase(it);
                return;
            }
        }
    }

    void on_node_created(
        dwpdsim::NodeId node_id,
        std::optional<dwpdsim::NodeId>,
        const dwpdsim::RadixTree& tree
    ) override {
        created_ids.push_back(node_id);
        creation_access_counts.push_back(tree.node(node_id).access_count);
    }

    void on_node_removed(
        dwpdsim::NodeId node_id,
        std::optional<dwpdsim::NodeId>,
        const dwpdsim::RadixTree& tree
    ) override {
        removed_ids.push_back(node_id);
        removed_was_absent.push_back(!tree.contains(node_id));
    }

    void on_access_complete(
        const dwpdsim::AccessContext& context,
        dwpdsim::AccessResult,
        const dwpdsim::RadixTree& tree
    ) override {
        completed_access_counts.push_back(tree.node(context.node_id).access_count);
    }

    std::vector<std::uint64_t> decision_access_counts;
    std::vector<dwpdsim::NodeId> created_ids;
    std::vector<std::uint64_t> creation_access_counts;
    std::vector<dwpdsim::NodeId> removed_ids;
    std::vector<bool> removed_was_absent;
    std::vector<std::uint64_t> completed_access_counts;
    bool decision_tree_visible = false;

  private:
    std::vector<dwpdsim::NodeId> resident_;
};

class SelectivePersistMemoryPolicy final : public dwpdsim::MemoryPolicy {
  public:
    bool admit_storage_hit(
        const dwpdsim::AccessContext&,
        const dwpdsim::Node&,
        const dwpdsim::RadixTree&
    ) override {
        return true;
    }

    dwpdsim::NodeId choose_victim(
        const dwpdsim::AccessContext&,
        const dwpdsim::RadixTree& tree
    ) override {
        return tree.segment_leaf_for(resident_.front());
    }

    dwpdsim::EvictionAction eviction_action(
        const dwpdsim::Node& victim,
        const dwpdsim::AccessContext&,
        const dwpdsim::RadixTree&
    ) override {
        return victim.hash_id == 1 ? dwpdsim::EvictionAction::Persist
                                   : dwpdsim::EvictionAction::Drop;
    }

    void on_memory_insert(dwpdsim::NodeId node_id) override {
        resident_.push_back(node_id);
    }

    void on_memory_access(dwpdsim::NodeId node_id) override {
        remove(node_id);
        resident_.push_back(node_id);
    }

    void on_memory_remove(dwpdsim::NodeId node_id) override {
        remove(node_id);
    }

  private:
    void remove(dwpdsim::NodeId node_id) {
        for (auto it = resident_.begin(); it != resident_.end(); ++it) {
            if (*it == node_id) {
                resident_.erase(it);
                return;
            }
        }
    }

    std::vector<dwpdsim::NodeId> resident_;
};

dwpdsim::SimulationConfig config() {
    return dwpdsim::SimulationConfig{
        8,
        8,
        dwpdsim::MediumConfig{8, 2},
        dwpdsim::MediumConfig{16, 1},
        "ticks",
        0,
    };
}

void test_promotion_drives_trim_before_write() {
    const std::filesystem::path trace_path =
        std::filesystem::temp_directory_path() / "dwpdsim-core-integration.csv";

    dwpdsim::Simulator simulator(
        config(),
        std::make_unique<dwpdsim::LruMemoryPolicy>(
            true,
            dwpdsim::EvictionAction::Persist
        ),
        std::make_unique<dwpdsim::FixedPlacementPolicy>(dwpdsim::Medium::Slc, 1),
        std::make_unique<dwpdsim::LruStorageEvictionPolicy>(),
        trace_path
    );

    simulator.process_request(0, std::vector<dwpdsim::HashId>{1});
    simulator.process_request(1, std::vector<dwpdsim::HashId>{2});
    simulator.process_request(2, std::vector<dwpdsim::HashId>{1});
    simulator.process_request(3, std::vector<dwpdsim::HashId>{1});
    simulator.finish();

    const dwpdsim::MetricsCollector& metrics = simulator.metrics();
    assert(metrics.request_count == 4);
    assert(metrics.block_access_count == 4);
    assert(metrics.global_misses == 2);
    assert(metrics.slc_hits == 1);
    assert(metrics.memory_hits == 1);
    assert(metrics.memory_evicted_segments == 2);
    assert(metrics.memory_evictions == 2);
    assert(metrics.memory_eviction_persists == 2);
    assert(metrics.io[0].reads == 1);
    assert(metrics.io[0].writes == 2);
    assert(metrics.io[0].trims == 1);
    assert(metrics.storage_evicted_segments[0] == 1);
    assert(metrics.storage_evicted_blocks[0] == 1);
    assert(metrics.io[0].stream_writes[1] == 2);
    assert(metrics.memory_resident_blocks == 1);
    assert(metrics.storage_resident_blocks[0] == 1);
    assert(metrics.duplicated_blocks == 0);
    assert(simulator.trace_event_count() == 4);

    const dwpdsim::NodeId first = *simulator.tree().find_root_child(1);
    const dwpdsim::NodeId second = *simulator.tree().find_root_child(2);
    assert(simulator.tree().node(first).in_memory);
    assert(!simulator.tree().node(first).on_storage);
    assert(!simulator.tree().node(second).in_memory);
    assert(simulator.tree().node(second).on_storage);

    std::ifstream trace(trace_path);
    std::vector<std::string> lines;
    for (std::string line; std::getline(trace, line);) {
        lines.push_back(std::move(line));
    }
    assert(lines.size() == 5);
    assert(lines[1].find(",WRITE,SLC,1,") != std::string::npos);
    assert(lines[2].find(",READ,SLC,1,") != std::string::npos);
    assert(lines[3].find(",TRIM,SLC,1,") != std::string::npos);
    assert(lines[4].find(",WRITE,SLC,1,") != std::string::npos);
    std::filesystem::remove(trace_path);
}

void test_storage_hit_can_bypass_memory() {
    const std::filesystem::path trace_path =
        std::filesystem::temp_directory_path() / "dwpdsim-core-bypass.csv";
    dwpdsim::SimulationConfig test_config = config();
    test_config.slc.capacity_bytes = 16;

    dwpdsim::Simulator simulator(
        test_config,
        std::make_unique<dwpdsim::LruMemoryPolicy>(
            false,
            dwpdsim::EvictionAction::Persist
        ),
        std::make_unique<dwpdsim::FixedPlacementPolicy>(dwpdsim::Medium::Slc, 0),
        std::make_unique<dwpdsim::LruStorageEvictionPolicy>(),
        trace_path
    );

    simulator.process_request(0, std::vector<dwpdsim::HashId>{10});
    simulator.process_request(1, std::vector<dwpdsim::HashId>{20});
    simulator.process_request(2, std::vector<dwpdsim::HashId>{10});
    simulator.finish();

    const dwpdsim::NodeId ten = *simulator.tree().find_root_child(10);
    const dwpdsim::NodeId twenty = *simulator.tree().find_root_child(20);
    assert(!simulator.tree().node(ten).in_memory);
    assert(simulator.tree().node(ten).on_storage);
    assert(simulator.tree().node(twenty).in_memory);
    assert(simulator.metrics().storage_bypasses == 1);
    assert(simulator.metrics().storage_promotions == 0);
    assert(simulator.metrics().io[0].reads == 1);
    std::filesystem::remove(trace_path);
}

void test_memory_and_storage_evict_complete_segments() {
    const std::filesystem::path trace_path =
        std::filesystem::temp_directory_path() / "dwpdsim-core-segments.csv";
    dwpdsim::SimulationConfig test_config = config();
    test_config.memory_capacity_bytes = 24;
    test_config.slc.capacity_bytes = 24;

    dwpdsim::Simulator simulator(
        test_config,
        std::make_unique<dwpdsim::LruMemoryPolicy>(
            true,
            dwpdsim::EvictionAction::Persist
        ),
        std::make_unique<dwpdsim::FixedPlacementPolicy>(dwpdsim::Medium::Slc, 0),
        std::make_unique<dwpdsim::LruStorageEvictionPolicy>(),
        trace_path
    );

    simulator.process_request(0, std::vector<dwpdsim::HashId>{1, 2, 3});
    simulator.process_request(1, std::vector<dwpdsim::HashId>{4});

    assert(simulator.metrics().memory_evicted_segments == 1);
    assert(simulator.metrics().memory_evictions == 3);
    assert(simulator.metrics().memory_eviction_persists == 3);
    assert(simulator.metrics().io[0].writes == 3);
    assert(simulator.metrics().storage_resident_blocks[0] == 3);

    simulator.process_request(2, std::vector<dwpdsim::HashId>{5});
    simulator.process_request(3, std::vector<dwpdsim::HashId>{6});
    simulator.process_request(4, std::vector<dwpdsim::HashId>{7});
    simulator.finish();

    assert(simulator.metrics().storage_evicted_segments[0] == 1);
    assert(simulator.metrics().storage_evicted_blocks[0] == 3);
    assert(simulator.metrics().io[0].trims == 3);
    assert(simulator.metrics().io[0].writes == 4);
    assert(simulator.metrics().storage_resident_blocks[0] == 1);

    std::ifstream trace(trace_path);
    std::vector<std::string> lines;
    for (std::string line; std::getline(trace, line);) {
        lines.push_back(std::move(line));
    }
    assert(lines.size() == 8);
    assert(lines[4].find(",TRIM,SLC,0,") != std::string::npos);
    assert(lines[5].find(",TRIM,SLC,0,") != std::string::npos);
    assert(lines[6].find(",TRIM,SLC,0,") != std::string::npos);
    assert(lines[7].find(",WRITE,SLC,0,") != std::string::npos);
    std::filesystem::remove(trace_path);
}

void test_policy_tree_view_and_deleted_node_lifecycle() {
    const std::filesystem::path trace_path =
        std::filesystem::temp_directory_path() / "dwpdsim-core-policy-tree.csv";
    auto memory_policy = std::make_unique<RecordingMemoryPolicy>();
    RecordingMemoryPolicy* recording = memory_policy.get();

    dwpdsim::Simulator simulator(
        config(),
        std::move(memory_policy),
        std::make_unique<dwpdsim::FixedPlacementPolicy>(dwpdsim::Medium::Slc, 0),
        std::make_unique<dwpdsim::LruStorageEvictionPolicy>(),
        trace_path
    );

    simulator.process_request(0, std::vector<dwpdsim::HashId>{9});
    simulator.process_request(1, std::vector<dwpdsim::HashId>{10});
    simulator.process_request(2, std::vector<dwpdsim::HashId>{9});
    simulator.finish();

    assert(recording->decision_tree_visible);
    assert((recording->decision_access_counts == std::vector<std::uint64_t>{1, 1}));
    assert((recording->created_ids == std::vector<dwpdsim::NodeId>{9, 10, 9}));
    assert((recording->creation_access_counts == std::vector<std::uint64_t>{0, 0, 0}));
    assert((recording->removed_ids == std::vector<dwpdsim::NodeId>{9, 10}));
    assert((recording->removed_was_absent == std::vector<bool>{true, true}));
    assert((recording->completed_access_counts == std::vector<std::uint64_t>{1, 1, 1}));
    assert(simulator.tree().node(9).first_seen_timestamp == 2);
    assert(simulator.tree().node(9).access_count == 1);
    assert(simulator.metrics().tree_nodes_created == 3);
    assert(simulator.metrics().tree_nodes_removed == 2);
    assert(simulator.tree().size() == 1);
    std::filesystem::remove(trace_path);
}

void test_branch_node_is_an_evictable_segment_endpoint() {
    const std::filesystem::path trace_path =
        std::filesystem::temp_directory_path() / "dwpdsim-core-branch-endpoint.csv";
    dwpdsim::SimulationConfig test_config = config();
    test_config.slc.capacity_bytes = 32;

    dwpdsim::Simulator simulator(
        test_config,
        std::make_unique<dwpdsim::LruMemoryPolicy>(
            true,
            dwpdsim::EvictionAction::Persist
        ),
        std::make_unique<dwpdsim::FixedPlacementPolicy>(dwpdsim::Medium::Slc, 0),
        std::make_unique<dwpdsim::LruStorageEvictionPolicy>(),
        trace_path
    );

    simulator.process_request(0, std::vector<dwpdsim::HashId>{1, 2});
    simulator.process_request(1, std::vector<dwpdsim::HashId>{1, 3});
    simulator.finish();

    assert(simulator.tree().child_count(1) == 2);
    assert(!simulator.tree().node(1).in_memory);
    assert(simulator.tree().node(1).on_storage);
    assert(simulator.tree().node(3).in_memory);
    assert(simulator.metrics().memory_resident_blocks == 1);
    assert(simulator.metrics().memory_evicted_segments == 3);
    std::filesystem::remove(trace_path);
}

void test_storage_eviction_uses_only_the_target_medium_subset() {
    const std::filesystem::path trace_path =
        std::filesystem::temp_directory_path() / "dwpdsim-core-medium-subset.csv";
    dwpdsim::SimulationConfig test_config = config();
    test_config.memory_capacity_bytes = 24;
    test_config.slc.capacity_bytes = 16;
    test_config.tlc.capacity_bytes = 16;

    dwpdsim::Simulator simulator(
        test_config,
        std::make_unique<dwpdsim::LruMemoryPolicy>(
            true,
            dwpdsim::EvictionAction::Persist
        ),
        std::make_unique<dwpdsim::RatioPlacementPolicy>(0.5, 2, 1),
        std::make_unique<dwpdsim::LruStorageEvictionPolicy>(),
        trace_path
    );

    simulator.process_request(0, std::vector<dwpdsim::HashId>{1, 2, 3});
    for (dwpdsim::HashId node_id = 4; node_id <= 8; ++node_id) {
        simulator.process_request(node_id, std::vector<dwpdsim::HashId>{node_id});
    }
    simulator.finish();

    assert(simulator.metrics().storage_evicted_segments[0] == 1);
    assert(simulator.metrics().storage_evicted_blocks[0] == 2);
    assert(simulator.metrics().storage_evicted_segments[1] == 0);
    assert(simulator.metrics().storage_evicted_blocks[1] == 0);
    assert(!simulator.tree().contains(3));
    assert(simulator.tree().node(2).on_storage);
    assert(simulator.tree().node(2).storage_medium == dwpdsim::Medium::Tlc);
    assert(!simulator.tree().node(1).on_storage);
    std::filesystem::remove(trace_path);
}

void test_memory_segment_keeps_per_block_copy_actions() {
    const std::filesystem::path trace_path =
        std::filesystem::temp_directory_path() / "dwpdsim-core-mixed-copies.csv";
    dwpdsim::SimulationConfig test_config = config();
    test_config.memory_capacity_bytes = 24;
    test_config.slc.capacity_bytes = 32;

    dwpdsim::Simulator simulator(
        test_config,
        std::make_unique<SelectivePersistMemoryPolicy>(),
        std::make_unique<dwpdsim::FixedPlacementPolicy>(dwpdsim::Medium::Slc, 0),
        std::make_unique<dwpdsim::LruStorageEvictionPolicy>(),
        trace_path
    );

    simulator.process_request(0, std::vector<dwpdsim::HashId>{1, 2, 3});
    simulator.process_request(1, std::vector<dwpdsim::HashId>{4});
    assert(simulator.metrics().io[0].writes == 1);

    simulator.process_request(2, std::vector<dwpdsim::HashId>{1, 2, 3});
    simulator.process_request(3, std::vector<dwpdsim::HashId>{5});
    simulator.finish();

    assert(simulator.metrics().memory_evicted_segments == 3);
    assert(simulator.metrics().memory_evictions == 7);
    assert(simulator.metrics().memory_evictions_with_storage_copy == 1);
    assert(simulator.metrics().memory_eviction_persists == 1);
    assert(simulator.metrics().memory_eviction_drops == 5);
    assert(simulator.metrics().io[0].writes == 1);
    std::filesystem::remove(trace_path);
}

}  // namespace

int main() {
    test_promotion_drives_trim_before_write();
    test_storage_hit_can_bypass_memory();
    test_memory_and_storage_evict_complete_segments();
    test_policy_tree_view_and_deleted_node_lifecycle();
    test_branch_node_is_an_evictable_segment_endpoint();
    test_storage_eviction_uses_only_the_target_medium_subset();
    test_memory_segment_keeps_per_block_copy_actions();
    return 0;
}
