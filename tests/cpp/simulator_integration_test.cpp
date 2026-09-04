#include <cassert>
#include <filesystem>
#include <fstream>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/policies.hpp"
#include "dwpdsim/simulator.hpp"
#include "dwpdsim/types.hpp"

namespace {

using dwpdsim::AffinityId;
using dwpdsim::HashId;
using dwpdsim::MemoryConfig;
using dwpdsim::RequestId;
using dwpdsim::SimulationConfig;
using dwpdsim::StorageTier;
using dwpdsim::StorageTierConfig;
using dwpdsim::TimestampNs;

constexpr std::uint64_t kBlockSize = 8;
constexpr TimestampNs kSecond = 1'000'000'000ULL;

SimulationConfig config(
    std::uint64_t memory_blocks,
    std::uint64_t slc_blocks,
    std::uint64_t tlc_blocks,
    std::optional<TimestampNs> simulation_end_ns = std::nullopt
) {
    return SimulationConfig{
        kBlockSize,
        MemoryConfig{memory_blocks * kBlockSize},
        StorageTierConfig{slc_blocks * kBlockSize, 2},
        StorageTierConfig{tlc_blocks * kBlockSize, 2},
        simulation_end_ns,
        0,
    };
}

std::vector<std::string> read_lines(const std::filesystem::path& path) {
    std::ifstream input(path);
    std::vector<std::string> lines;
    for (std::string line; std::getline(input, line);) {
        lines.push_back(std::move(line));
    }
    return lines;
}

void process(
    dwpdsim::Simulator& simulator,
    TimestampNs timestamp_ns,
    RequestId request_id,
    std::vector<HashId> hashes,
    AffinityId affinity_id = 1
) {
    simulator.process_request(timestamp_ns, request_id, affinity_id, hashes);
}

class ScriptedMemoryPolicy final : public dwpdsim::MemoryPolicy {
  public:
    explicit ScriptedMemoryPolicy(std::vector<dwpdsim::MemoryEvictionDecision> decisions)
        : decisions_(std::move(decisions)) {}

    bool admit_storage_hit(
        const dwpdsim::AccessContext&,
        const dwpdsim::Node&,
        const dwpdsim::RadixTree&
    ) const override {
        return true;
    }

    dwpdsim::MemoryEvictionDecision evict(
        const dwpdsim::RequestContext&,
        const dwpdsim::RadixTree&
    ) const override {
        assert(next_decision_ < decisions_.size());
        return decisions_[next_decision_++];
    }

    void on_commit(const dwpdsim::MemoryMutation&) override {}

  private:
    std::vector<dwpdsim::MemoryEvictionDecision> decisions_;
    mutable std::size_t next_decision_ = 0;
};

void drop_prunes_only_the_selected_leaf_segment() {
    const auto trace = std::filesystem::temp_directory_path() /
                       "dwpdsim-memory-policy-drop.csv";
    dwpdsim::Simulator simulator(
        config(3, 8, 4),
        std::make_unique<ScriptedMemoryPolicy>(
            std::vector<dwpdsim::MemoryEvictionDecision>{
                {2, dwpdsim::MemoryEvictionAction::Dump},
                {3, dwpdsim::MemoryEvictionAction::Dump},
                {2, dwpdsim::MemoryEvictionAction::Drop},
            }
        ),
        std::make_unique<dwpdsim::BaselineFixedLruStoragePolicy>(
            dwpdsim::Placement{StorageTier::Slc, 0}
        ),
        trace
    );

    process(simulator, 0, 1, {1, 2});
    process(simulator, 1, 2, {1, 3});
    process(simulator, 2, 3, {4});
    process(simulator, 3, 4, {1, 2});
    process(simulator, 4, 5, {5});
    simulator.finish();

    const auto& metrics = simulator.metrics();
    assert(metrics.memory_drop_segments == 1);
    assert(metrics.memory_drop_blocks == 1);
    assert(metrics.dump_requests == 2);
    assert(metrics.io[0].writes == 2);
    assert(simulator.tree().node(1).in_memory);
    assert(!simulator.tree().node(2).in_memory);
    assert(simulator.tree().node(2).on_storage);
    std::filesystem::remove(trace);
}

void dump_is_one_atomic_segment_admission() {
    const auto trace = std::filesystem::temp_directory_path() /
                       "dwpdsim-vnext-atomic-dump.csv";
    dwpdsim::Simulator simulator(
        config(2, 1, 4),
        std::make_unique<dwpdsim::BaselineMemoryLruPolicy>(true),
        std::make_unique<dwpdsim::BaselineFixedLruStoragePolicy>(
            dwpdsim::Placement{StorageTier::Slc, 1}
        ),
        trace
    );

    process(simulator, 0, 1, {10, 20});
    process(simulator, 1, 2, {30});
    simulator.finish();

    const auto& metrics = simulator.metrics();
    assert(metrics.dump_requests == 1);
    assert(metrics.dumps_admitted.segments == 0);
    assert(metrics.dumps_rejected.segments == 1);
    assert(metrics.dumps_rejected.blocks == 2);
    assert(metrics.admission_rejections == 1);
    assert(metrics.io[0].writes == 0);
    assert(simulator.tree().size() == 1);
    assert(read_lines(trace).size() == 1);
    std::filesystem::remove(trace);
}

void dump_reclaim_is_leaf_first_and_greedy_by_segment() {
    const auto trace = std::filesystem::temp_directory_path() /
                       "dwpdsim-memory-segment-reclaim.csv";
    dwpdsim::Simulator simulator(
        config(3, 16, 4),
        std::make_unique<dwpdsim::BaselineMemoryLruPolicy>(true),
        std::make_unique<dwpdsim::BaselineFixedLruStoragePolicy>(
            dwpdsim::Placement{StorageTier::Slc, 1}
        ),
        trace
    );

    process(simulator, 0, 1, {1, 2, 4});
    process(simulator, 1, 2, {1, 3});
    process(simulator, 2, 3, {1, 2, 4});
    process(simulator, 3, 4, {5});
    simulator.finish();

    const auto& metrics = simulator.metrics();
    assert(metrics.memory_evicted_segments == 4);
    assert(metrics.memory_evicted_blocks == 6);
    assert(metrics.memory_evictions_with_storage_copy == 2);
    assert(metrics.memory_dump_segments == 3);
    assert(metrics.memory_dump_blocks == 4);
    assert(metrics.dump_requests == 3);
    assert(metrics.io[0].writes == 4);
    assert(metrics.io[0].reads == 2);
    assert(!simulator.tree().node(1).in_memory);
    assert(simulator.tree().node(1).on_storage);
    assert(!simulator.tree().node(2).in_memory);
    assert(simulator.tree().node(2).on_storage);
    assert(!simulator.tree().node(4).in_memory);
    assert(simulator.tree().node(4).on_storage);
    assert(simulator.tree().node(5).in_memory);

    const auto lines = read_lines(trace);
    assert(lines.size() == 7);
    assert(lines.back().find(",1,1,MEMORY_DUMP,,") != std::string::npos);
    std::filesystem::remove(trace);
}

void memory_lru_tracks_segment_accesses() {
    const auto trace = std::filesystem::temp_directory_path() /
                       "dwpdsim-memory-segment-lru.csv";
    dwpdsim::Simulator simulator(
        config(3, 8, 4),
        std::make_unique<dwpdsim::BaselineMemoryLruPolicy>(),
        std::make_unique<dwpdsim::BaselineFixedLruStoragePolicy>(
            dwpdsim::Placement{StorageTier::Slc, 0}
        ),
        trace
    );

    process(simulator, 0, 1, {1, 2});
    process(simulator, 1, 2, {3});
    process(simulator, 2, 3, {1});
    process(simulator, 3, 4, {4});
    simulator.finish();

    assert(simulator.tree().node(1).in_memory);
    assert(simulator.tree().node(2).in_memory);
    assert(!simulator.tree().node(3).in_memory);
    assert(simulator.tree().node(3).on_storage);
    assert(simulator.tree().node(4).in_memory);
    const auto lines = read_lines(trace);
    assert(lines.size() == 2);
    assert(lines.back().find(",3,3,MEMORY_DUMP,,") != std::string::npos);
    std::filesystem::remove(trace);
}

void baseline_dump_and_storage_hit_use_new_interfaces() {
    const auto trace = std::filesystem::temp_directory_path() /
                       "dwpdsim-vnext-baseline.csv";
    dwpdsim::Simulator simulator(
        config(1, 4, 4),
        std::make_unique<dwpdsim::BaselineMemoryLruPolicy>(true),
        std::make_unique<dwpdsim::BaselineFixedLruStoragePolicy>(
            dwpdsim::Placement{StorageTier::Slc, 1}
        ),
        trace
    );

    process(simulator, 0, 1, {1});
    process(simulator, 1, 2, {2});
    process(simulator, 2, 3, {1});
    simulator.finish();

    const auto& metrics = simulator.metrics();
    assert(metrics.dumps_admitted.segments == 2);
    assert(metrics.io[0].writes == 2);
    assert(metrics.io[0].reads == 1);
    assert(metrics.slc_hits == 1);
    const auto lines = read_lines(trace);
    assert(lines.size() == 4);
    assert(lines[0].find("depends_on_sequence") != std::string::npos);
    assert(lines[1].find(",WRITE,SLC,1,") != std::string::npos);
    assert(lines[1].find(",MEMORY_DUMP,,") != std::string::npos);
    assert(lines[2].find(",READ,SLC,1,") != std::string::npos);
    assert(lines[2].find(",STORAGE_HIT,,") != std::string::npos);
    std::filesystem::remove(trace);
}

dwpdsim::AdaptiveEndurancePolicyConfig adaptive_endurance_config(TimestampNs period_ns) {
    dwpdsim::AdaptiveEndurancePolicyConfig policy;
    policy.logical_fill_fraction = 1.0;
    policy.background_period_ns = period_ns;
    policy.promotion_seconds = 2.0;
    policy.idle_multiplier = 1e9;
    return policy;
}

void background_relocation_emits_explicit_read_write_trim_chain() {
    const auto trace = std::filesystem::temp_directory_path() /
                       "dwpdsim-vnext-background.csv";
    dwpdsim::Simulator simulator(
        config(1, 4, 4, kSecond),
        std::make_unique<dwpdsim::BaselineMemoryLruPolicy>(true),
        std::make_unique<dwpdsim::AdaptiveEnduranceStoragePolicy>(
            adaptive_endurance_config(kSecond)
        ),
        trace
    );

    process(simulator, 0, 1, {1}, 77);
    process(simulator, 0, 2, {2}, 88);
    simulator.finish();

    const auto& metrics = simulator.metrics();
    assert(metrics.background_ticks == 1);
    assert(metrics.background_migrations.segments == 1);
    assert(metrics.relocation_explicit_read_blocks == 1);
    assert(metrics.relocation_reused_read_blocks == 0);
    assert(metrics.io[0].reads == 1);
    assert(metrics.io[1].writes == 1);
    assert(metrics.io[0].trims == 1);

    const auto lines = read_lines(trace);
    assert(lines.size() == 5);
    assert(lines[2].find(",READ,SLC,") != std::string::npos);
    assert(lines[2].find(",BACKGROUND_MIGRATION,1,") != std::string::npos);
    assert(lines[3].find(",WRITE,TLC,") != std::string::npos);
    assert(lines[3].find(",BACKGROUND_MIGRATION,1,1") != std::string::npos);
    assert(lines[4].find(",TRIM,SLC,") != std::string::npos);
    assert(lines[4].find(",BACKGROUND_MIGRATION,1,2") != std::string::npos);
    std::filesystem::remove(trace);
}

void access_relocation_reuses_storage_hit_read() {
    const auto trace = std::filesystem::temp_directory_path() /
                       "dwpdsim-vnext-access.csv";
    dwpdsim::Simulator simulator(
        config(1, 4, 8),
        std::make_unique<dwpdsim::BaselineMemoryLruPolicy>(true),
        std::make_unique<dwpdsim::AdaptiveEnduranceStoragePolicy>(
            adaptive_endurance_config(0)
        ),
        trace
    );

    process(simulator, 0, 1, {1}, 99);
    process(simulator, 0, 2, {2}, 100);
    process(simulator, kSecond, 3, {1}, 99);
    simulator.finish();

    const auto& metrics = simulator.metrics();
    assert(metrics.access_migrations.segments == 1);
    assert(metrics.relocation_reused_read_blocks == 1);
    assert(metrics.relocation_explicit_read_blocks == 0);
    assert(metrics.io[0].reads == 1);

    const auto lines = read_lines(trace);
    assert(lines[2].find(",READ,SLC,") != std::string::npos);
    assert(lines[2].find(",STORAGE_HIT,1,") != std::string::npos);
    assert(lines[3].find(",WRITE,TLC,") != std::string::npos);
    assert(lines[3].find(",ACCESS_MIGRATION,1,1") != std::string::npos);
    assert(lines[4].find(",TRIM,SLC,") != std::string::npos);
    assert(lines[4].find(",ACCESS_MIGRATION,1,2") != std::string::npos);
    std::filesystem::remove(trace);
}

void request_ids_are_unique_and_timestamps_are_monotonic() {
    const auto trace = std::filesystem::temp_directory_path() /
                       "dwpdsim-vnext-input-contract.csv";
    dwpdsim::Simulator simulator(
        config(2, 2, 2),
        std::make_unique<dwpdsim::BaselineMemoryLruPolicy>(),
        std::make_unique<dwpdsim::BaselineFixedLruStoragePolicy>(
            dwpdsim::Placement{StorageTier::Tlc, 0}
        ),
        trace
    );
    process(simulator, 10, 1, {1});
    bool duplicate_failed = false;
    try {
        process(simulator, 10, 1, {2});
    } catch (const std::invalid_argument&) {
        duplicate_failed = true;
    }
    assert(duplicate_failed);
    bool timestamp_failed = false;
    try {
        process(simulator, 9, 2, {2});
    } catch (const std::invalid_argument&) {
        timestamp_failed = true;
    }
    assert(timestamp_failed);
    simulator.finish();
    std::filesystem::remove(trace);
}

void configuration_rejects_more_than_eight_total_streams() {
    auto invalid_config = config(2, 2, 2);
    invalid_config.slc.stream_count = 5;
    invalid_config.tlc.stream_count = 4;
    bool failed = false;
    try {
        dwpdsim::Simulator simulator(
            invalid_config,
            std::make_unique<dwpdsim::BaselineMemoryLruPolicy>(),
            std::make_unique<dwpdsim::BaselineFixedLruStoragePolicy>(
                dwpdsim::Placement{StorageTier::Tlc, 0}
            ),
            std::filesystem::temp_directory_path() / "dwpdsim-vnext-stream-limit.csv"
        );
    } catch (const std::invalid_argument&) {
        failed = true;
    }
    assert(failed);
}

}  // namespace

int main() {
    drop_prunes_only_the_selected_leaf_segment();
    dump_is_one_atomic_segment_admission();
    dump_reclaim_is_leaf_first_and_greedy_by_segment();
    memory_lru_tracks_segment_accesses();
    baseline_dump_and_storage_hit_use_new_interfaces();
    background_relocation_emits_explicit_read_write_trim_chain();
    access_relocation_reuses_storage_hit_read();
    request_ids_are_unique_and_timestamps_are_monotonic();
    configuration_rejects_more_than_eight_total_streams();
    return 0;
}
