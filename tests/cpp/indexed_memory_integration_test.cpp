#include <cassert>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <random>
#include <string>

#include "dwpdsim/policies.hpp"
#include "dwpdsim/simulator.hpp"

using namespace dwpdsim;

std::string replay(const std::filesystem::path& path, std::size_t groups,
                   std::size_t workers, std::size_t sample, unsigned seed) {
    SimulationConfig config;
    config.block_size_bytes = 512;
    config.memory.capacity_bytes = 17 * 512;
    config.slc = {64 * 512, 1};
    config.tlc = {128 * 512, 1};
    std::unique_ptr<MemoryPolicy> memory;
    if (groups == 0) { memory = std::make_unique<BaselineMemoryLruPolicy>(true); }
    else { memory = std::make_unique<IndexedMemoryLruPolicy>(true, groups, sample, workers, 9); }
    Simulator sim(config, std::move(memory),
                  std::make_unique<BaselineFixedLruStoragePolicy>(Placement{StorageTier::Tlc, 0}), path);
    std::mt19937 rng(seed);
    for (std::uint64_t i = 0; i < 1500; ++i) {
        const std::uint64_t root = rng() % 24;
        const std::uint64_t branch = rng() % 8;
        std::vector<HashId> hashes{root * 1000, root * 1000 + 1};
        const auto length = 1 + rng() % 16;
        for (unsigned j = 0; j < length; ++j) {
            hashes.push_back(root * 1000 + 2 + branch * 20 + j);
        }
        sim.process_request(i / 7, i, i % 3, hashes);
    }
    sim.finish();
    if (workers > 1) { assert(sim.memory_policy_work().worker_rounds > 0); }
    std::ifstream input(path);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

void retention_preserves_hot_prefix(const std::filesystem::path& path) {
    SimulationConfig config;
    config.block_size_bytes = 512;
    config.memory.capacity_bytes = 4 * 512;
    config.slc = {16 * 512, 1};
    config.tlc = {16 * 512, 1};
    Simulator sim(config, std::make_unique<IndexedMemoryLruPolicy>(true, 7, 0, 3, 42, 5),
                  std::make_unique<BaselineFixedLruStoragePolicy>(Placement{StorageTier::Tlc, 0}), path);
    sim.process_request(0, 0, 0, std::vector<HashId>{1, 2, 3, 4});
    sim.process_request(9, 1, 0, std::vector<HashId>{1, 2, 5});
    sim.process_request(10, 2, 0, std::vector<HashId>{6});
    sim.process_request(11, 3, 0, std::vector<HashId>{7});
    sim.finish();
    assert(sim.metrics().memory_drop_blocks == 2);
    assert(sim.metrics().memory_dump_blocks == 3);
    assert(sim.trace_event_count() == 3);
    assert(sim.tree().node(1).on_storage && sim.tree().node(2).on_storage);
    assert(!sim.tree().contains(3) && !sim.tree().contains(4));
}

int main() {
    const auto directory = std::filesystem::temp_directory_path() / "dwpdsim-indexed-integration";
    std::filesystem::create_directories(directory);
    for (unsigned seed : {3, 29, 107}) {
        const auto baseline = replay(directory / "baseline.csv", 0, 1, 0, seed);
        assert(replay(directory / "single.csv", 1, 1, 0, seed) == baseline);
        assert(replay(directory / "grouped.csv", 11, 1, 0, seed) == baseline);
        assert(replay(directory / "parallel.csv", 11, 3, 0, seed) == baseline);
        assert(replay(directory / "sample.csv", 11, 1, 3, seed)
               == replay(directory / "parallel-sample.csv", 11, 3, 3, seed));
    }
    retention_preserves_hot_prefix(directory / "retention.csv");
    std::filesystem::remove_all(directory);
}
