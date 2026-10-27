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
    h.pool_allocation = "fixed";
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
    e.request_id = 3;
    e.hash_ids = {9, 2};
    bool rejected = false;
    try { sim.process_hybrid_event(e); }
    catch (const std::invalid_argument& error) {
        rejected = std::string(error.what()).find("different parent") != std::string::npos;
    }
    assert(rejected);
    sim.finish();
    std::filesystem::remove(path.string() + ".hybrid.csv");

    h.replay_mode = "prefix_replay";
    h.tokens_per_block = 1024;
    h.full_capacity_bytes = 128;
    h.linear_capacity_bytes = 32;
    cfg.memory.capacity_bytes = 160;
    Simulator prefill_sim(cfg, std::make_unique<BaselineMemoryLruPolicy>(true), nullptr, path);
    HybridPrefillRequest q;
    q.hash_ids = {1, 2, 3, 4, 5, 6, 7, 8, 9};
    q.input_tokens = 10000;
    const auto cold = prefill_sim.process_hybrid_prefill(q);
    assert(cold.restore_tokens == 0);
    assert(cold.generated_checkpoint_tokens == std::vector<std::uint64_t>({4096, 8192}));
    q.sequence = q.request_id = 1;
    const auto warm = prefill_sim.process_hybrid_prefill(q);
    assert(warm.restore_tokens == 8192);
    assert(warm.restore_hash_id == 8);
    assert(warm.prefill_compute_tokens == 1808);
    assert(warm.generated_checkpoint_tokens.empty());
    q.sequence = q.request_id = 2;
    q.hash_ids = {1, 2, 3, 4, 5, 6, 20, 21, 22};
    const auto fork = prefill_sim.process_hybrid_prefill(q);
    assert(fork.restore_tokens == 4096);
    assert(fork.generated_checkpoint_tokens == std::vector<std::uint64_t>({8192}));
    assert(prefill_sim.hybrid_component(8, 1)->demands == 1);
    assert(prefill_sim.hybrid_component(21, 1)->demands == 0);
    prefill_sim.finish();
    std::filesystem::remove(path.string() + ".hybrid.csv");

    h.checkpoint_generation = "request_end";
    Simulator endpoint_sim(cfg, std::make_unique<BaselineMemoryLruPolicy>(true), nullptr, path);
    q.sequence = q.request_id = 0;
    q.hash_ids = {1, 2, 3, 4, 5, 6, 7, 8, 9};
    const auto endpoint = endpoint_sim.process_hybrid_prefill(q);
    assert(endpoint.generated_checkpoint_tokens == std::vector<std::uint64_t>({9216}));
    assert(!endpoint_sim.hybrid_component(4, 1)->published);
    q.sequence = q.request_id = 1;
    const auto repeat = endpoint_sim.process_hybrid_prefill(q);
    assert(repeat.restore_tokens == 9216);
    assert(endpoint_sim.hybrid_component(9, 1)->demands == 1);
    assert(endpoint_sim.hybrid_component(4, 1)->demands == 0);
    endpoint_sim.finish();
    std::filesystem::remove(path.string() + ".hybrid.csv");
}
