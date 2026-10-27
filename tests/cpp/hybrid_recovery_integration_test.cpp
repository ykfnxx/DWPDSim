#include <cassert>
#include <cmath>
#include <filesystem>
#include <memory>

#include "dwpdsim/hybrid_recovery.hpp"
#include "dwpdsim/policies/memory/baseline_memory_lru_policy.hpp"
#include "dwpdsim/simulator.hpp"

using namespace dwpdsim;

int main() {
    SimulationConfig cfg;
    cfg.block_size_bytes = 8;
    cfg.memory.capacity_bytes = 64;
    cfg.infinite_storage = true;
    auto& h = cfg.hybrid_attention;
    h.enabled = true;
    h.full_block_bytes = 8;
    h.linear_checkpoint_bytes = 16;
    h.emit_io_trace = false;
    Simulator sim(cfg, std::make_unique<BaselineMemoryLruPolicy>(true), nullptr,
        std::filesystem::temp_directory_path() / "dwpdsim-cost-recovery");
    HybridEvent e;
    e.action = "publish";
    e.components = "full";
    for (const auto id : {1ULL, 2ULL, 3ULL}) {
        e.hash_ids.push_back(id);
        sim.process_hybrid_event(e);
        ++e.sequence;
    }
    e.components = "linear";
    e.hash_ids = {1, 2};
    sim.process_hybrid_event(e);

    const HybridRecoveryModel model(1024, 8, {100, 100}, 200, 1024);
    HybridPrefillRequest q;
    q.hash_ids = {1, 2, 3, 4};
    q.input_tokens = 4096;
    const auto ab = model.evaluate(q, HybridRecoveryState(sim.tree()));
    assert(ab.matched_tokens == 3072);
    assert(ab.full_available_tokens == 3072);
    assert(ab.restored_tokens == 2048); // FULL A/B/C plus LINEAR B restores A/B.

    ++e.sequence;
    e.hash_ids = {1, 2, 3};
    sim.process_hybrid_event(e);
    const auto before = model.evaluate(q, HybridRecoveryState(sim.tree()));
    const auto drop_full = model.evaluate(q, HybridRecoveryState(sim.tree(), {
        {1, 0, true, true, {}}}));
    const auto drop_linear = model.evaluate(q, HybridRecoveryState(sim.tree(), {
        {3, 1, true, true, {}}}));
    const auto drop_both = model.evaluate(q, HybridRecoveryState(sim.tree(), {
        {1, 0, true, true, {}}, {3, 1, true, true, {}}}));
    assert(before.restored_tokens == 3072);
    assert(drop_full.restored_tokens == 0);
    assert(drop_linear.restored_tokens == 2048);
    assert(drop_both.restored_tokens == 0);
    const auto joint_loss = before.restored_tokens - drop_both.restored_tokens;
    assert(joint_loss < (before.restored_tokens - drop_full.restored_tokens) +
        (before.restored_tokens - drop_linear.restored_tokens));

    const auto backed = model.evaluate(q, HybridRecoveryState(sim.tree(), {
        {1, 0, true, false, StorageTier::Tlc},
        {3, 1, true, false, StorageTier::Slc}}));
    assert(backed.restored_tokens == 3072);
    assert(backed.storage_read_bytes[0] == 16 && backed.storage_read_bytes[1] == 8);
    assert(backed.gpu_transfer_bytes == 40);
    assert(std::abs(backed.total_seconds - 1.36) < 1e-12);
    assert(sim.hybrid_component(1, 0)->in_dram);
    assert(sim.hybrid_component(3, 1)->in_dram);
    assert(sim.hybrid_stats().counters[0].write_objects == 0);
    assert(sim.hybrid_stats().counters[1].write_objects == 0);

    // The final prompt token must still execute; path traversal never creates a
    // checkpoint, and copy changes do not mutate actual simulator residency.
    q.hash_ids = {1, 2, 3};
    q.input_tokens = 3072;
    assert(model.evaluate(q, HybridRecoveryState(sim.tree())).restored_tokens == 2048);
    q.max_prefix_tokens = 1024;
    assert(model.evaluate(q, HybridRecoveryState(sim.tree())).restored_tokens == 0);
    sim.finish();
}
