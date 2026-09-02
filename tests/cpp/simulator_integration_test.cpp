#include <cassert>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/policies.hpp"
#include "dwpdsim/simulator.hpp"
#include "dwpdsim/types.hpp"

namespace {

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
    assert(metrics.memory_evictions == 2);
    assert(metrics.memory_eviction_persists == 2);
    assert(metrics.io[0].reads == 1);
    assert(metrics.io[0].writes == 2);
    assert(metrics.io[0].trims == 1);
    assert(metrics.io[0].stream_writes[1] == 2);
    assert(metrics.memory_resident_blocks == 1);
    assert(metrics.storage_resident_blocks[0] == 1);
    assert(metrics.duplicated_blocks == 0);
    assert(simulator.trace_event_count() == 4);

    const dwpdsim::NodeId first = *simulator.tree().find_child(dwpdsim::kRootNodeId, 1);
    const dwpdsim::NodeId second = *simulator.tree().find_child(dwpdsim::kRootNodeId, 2);
    assert(simulator.tree().node(first).in_memory);
    assert(!simulator.tree().node(first).storage_location.has_value());
    assert(!simulator.tree().node(second).in_memory);
    assert(simulator.tree().node(second).storage_location.has_value());

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

    const dwpdsim::NodeId ten = *simulator.tree().find_child(dwpdsim::kRootNodeId, 10);
    const dwpdsim::NodeId twenty = *simulator.tree().find_child(dwpdsim::kRootNodeId, 20);
    assert(!simulator.tree().node(ten).in_memory);
    assert(simulator.tree().node(ten).storage_location.has_value());
    assert(simulator.tree().node(twenty).in_memory);
    assert(simulator.metrics().storage_bypasses == 1);
    assert(simulator.metrics().storage_promotions == 0);
    assert(simulator.metrics().io[0].reads == 1);
    std::filesystem::remove(trace_path);
}

}  // namespace

int main() {
    test_promotion_drives_trim_before_write();
    test_storage_hit_can_bypass_memory();
    return 0;
}
