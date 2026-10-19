#include <cassert>
#include <filesystem>
#include <memory>
#include <stdexcept>

#include "dwpdsim/policies/memory/baseline_memory_lru_policy.hpp"
#include "dwpdsim/simulator.hpp"

int main() {
    using namespace dwpdsim;
    SimulationConfig cfg;
    cfg.block_size_bytes = 8;
    cfg.memory.capacity_bytes = 64;
    cfg.infinite_storage = true;
    auto& h = cfg.hybrid_attention;
    h.enabled = true;
    h.full_capacity_bytes = 32;
    h.linear_capacity_bytes = 32;
    h.full_block_bytes = 8;
    h.linear_checkpoint_bytes = 16;
    const auto path = std::filesystem::temp_directory_path() / "dwpdsim-hybrid-integration";
    Simulator sim(cfg, std::make_unique<BaselineMemoryLruPolicy>(true), nullptr, path);
    HybridEvent e;
    e.components = "linear";
    e.action = "publish";
    e.hash_ids = {1};
    sim.process_hybrid_event(e);
    e.sequence = 1;
    e.hash_ids = {1, 2};
    sim.process_hybrid_event(e);
    e.sequence = 2;
    e.action = "request";
    e.request_id = 1;
    e.hash_ids = {1};
    assert(sim.process_hybrid_event(e)->linear == "dram");
    e.sequence = 3;
    e.action = "publish";
    e.hash_ids = {1, 2, 3};
    sim.process_hybrid_event(e);
    assert(!sim.hybrid_component(2, 1)->in_dram);
    assert(!sim.hybrid_component(2, 1)->storage_addresses.empty());
    assert(sim.hybrid_component(3, 1)->in_dram);
    assert(sim.hybrid_component(2, 1)->demands == 0);
    e.sequence = 4;
    e.action = "request";
    e.request_id = 2;
    e.components = "both";
    e.hash_ids = {1, 2};
    const auto result = sim.process_hybrid_event(e);
    assert(result->linear == "storage");
    assert(result->full == std::vector<std::string>({"never_published", "never_published"}));
    assert(!result->all_available);
    assert(sim.hybrid_stats().joint_available == 0);
    assert(sim.hybrid_stats().counters[1].read_bytes == 16);
    e.sequence = 5;
    e.hash_ids = {9, 2};
    bool rejected = false;
    try { sim.process_hybrid_event(e); }
    catch (const std::invalid_argument&) { rejected = true; }
    assert(rejected);
    assert(!sim.tree().contains(9));
    sim.finish();
    std::filesystem::remove(path.string() + ".hybrid.csv");
}
