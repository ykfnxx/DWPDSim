#include <cassert>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <random>
#include <string>

#include "dwpdsim/policies.hpp"
#include "dwpdsim/simulator.hpp"

using namespace dwpdsim;

std::string replay(const std::filesystem::path& path, const std::string& mode,
                   bool counts, unsigned seed) {
    SimulationConfig config;
    config.block_size_bytes = 512;
    config.memory.capacity_bytes = 17 * 512;
    config.slc = {64 * 512, 3};
    config.tlc = {96 * 512, 2};
    WearShareRoundRobinPolicyConfig rr;
    rr.victim_search = mode;
    rr.subtree_counts = counts;
    rr.verify_victims = true;
    Simulator sim(config, std::make_unique<IndexedMemoryLruPolicy>(false, 1, 0, 1, 0),
                  std::make_unique<WearShareRoundRobinStoragePolicy>(rr), path);
    std::mt19937 rng(seed);
    for (std::uint64_t i = 0; i < 2000; ++i) {
        const auto root = i % 9 == 0 ? 32 + i : rng() % 32;
        const auto branch = rng() % 4;
        std::vector<HashId> hashes{root * 1000, root * 1000 + 1};
        for (unsigned j = 0, length = rng() % 16; j < length; ++j) {
            hashes.push_back(root * 1000 + 100 + branch * 20 + j);
        }
        sim.process_request(i / 5, i, i % 7, hashes);
    }
    sim.finish();
    const auto work = sim.storage_policy_work();
    assert(work.decisions > 0 && work.decisions == work.verified_decisions);
    std::ifstream input(path);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

void storage_clock(const std::filesystem::path& path) {
    SimulationConfig config;
    config.block_size_bytes = 512;
    config.memory.capacity_bytes = 2 * 512;
    config.slc = {64 * 512, 2};
    config.tlc = {64 * 512, 2};
    Simulator sim(config, std::make_unique<IndexedMemoryLruPolicy>(true, 1, 0, 1, 0),
                  std::make_unique<WearShareRoundRobinStoragePolicy>(WearShareRoundRobinPolicyConfig{}), path);
    sim.process_request(0, 0, 0, std::vector<HashId>{1, 2});
    sim.process_request(1, 1, 0, std::vector<HashId>{3, 4});
    assert(sim.tree().node(1).on_storage && sim.tree().node(2).on_storage);
    sim.process_request(7, 2, 0, std::vector<HashId>{1});
    assert(sim.tree().node(1).storage_last_access_timestamp_ns == 7);
    assert(sim.tree().node(2).storage_last_access_timestamp_ns == 7);
    assert(sim.tree().node(2).last_access_timestamp_ns == 0);
    sim.process_request(9, 3, 0, std::vector<HashId>{1});
    assert(sim.tree().node(1).last_access_timestamp_ns == 9);
    assert(sim.tree().node(1).storage_last_access_timestamp_ns == 7);
    sim.finish();
}

int main() {
    const auto dir = std::filesystem::temp_directory_path() / "dwpdsim-rr-integration";
    std::filesystem::create_directories(dir);
    for (unsigned seed : {3, 19, 71}) {
        const auto expected = replay(dir / "scan.csv", "scan", false, seed);
        assert(replay(dir / "fused.csv", "fused", false, seed) == expected);
        assert(replay(dir / "index.csv", "indexed", false, seed) == expected);
        assert(replay(dir / "counts.csv", "indexed", true, seed) == expected);
    }
    storage_clock(dir / "clock.csv");
    std::filesystem::remove_all(dir);
}
