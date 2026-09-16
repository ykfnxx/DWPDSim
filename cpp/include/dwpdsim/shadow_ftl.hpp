#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <set>
#include <unordered_map>
#include <vector>

#include "dwpdsim/config.hpp"

namespace dwpdsim {

struct ShadowConfig {
    bool online_tuning = false;
    TimestampNs feedback_period_ns = 900ULL * 1000000000ULL;
    std::uint64_t page_bytes = 4096;
    std::uint64_t pages_per_block = 256;
    double overprovisioning = 0.07;
    // Zero derives nominal device capacity from the corresponding storage tier.
    std::uint64_t slc_nominal_bytes = 0;
    std::uint64_t tlc_nominal_bytes = 0;
    double slc_endurance = 120;
    double tlc_endurance = 12;
    double reuse_loss_budget = 0.01;
    // All reuse samples are KV blocks, not tokens or NAND pages.
    std::uint64_t min_reuse_blocks = 4096;
    std::uint64_t reuse_ema_scale_blocks = 50000;
    // Zero derives physical geometry from nominal capacity and overprovisioning.
    std::uint64_t slc_physical_blocks = 0;
    std::uint64_t tlc_physical_blocks = 0;
};

struct ShadowPoolStats {
    std::uint64_t host_program_pages = 0;
    std::uint64_t gc_program_pages = 0;
    std::uint64_t erases = 0;
    std::uint64_t physical_blocks = 0;
    std::uint64_t nominal_bytes = 0;
    double wa_ema = 1;
    double lifetime_pressure = 0;
};

struct ShadowFeedback {
    TimestampNs timestamp_ns = 0;
    std::array<ShadowPoolStats, 2> pools;
    std::uint64_t hit_blocks = 0;
    std::uint64_t ghost_miss_blocks = 0;
};

// Synchronous program/GC estimator. It does not simulate I/O latency or queues.
class ShadowFtl {
  public:
    ShadowFtl(const SimulationConfig& simulation, const ShadowConfig& config);
    ~ShadowFtl();
    void consume(Operation op, const StorageLocation& location, TimestampNs now);
    void record_loss(NodeId id, TimestampNs now);
    std::uint64_t ghost_prefix(const HashId* hashes, std::size_t size, TimestampNs now);
    void record_request(std::uint64_t hits, std::uint64_t ghost_misses);
    ShadowFeedback close_window(TimestampNs now);
    ShadowFeedback stats() const;
    const ShadowConfig& config() const { return config_; }
    std::uint64_t ghost_entries() const { return ghosts_.size(); }
    std::uint64_t total_ghost_misses() const { return total_ghost_misses_; }

  private:
    class Pool;
    void expire_ghosts(TimestampNs now);
    ShadowConfig config_;
    std::uint64_t pages_per_kv_block_;
    std::array<std::unique_ptr<Pool>, 2> pools_;
    std::array<std::uint64_t, 2> last_host_{};
    std::array<std::uint64_t, 2> last_nand_{};
    std::array<double, 2> wa_{1, 1};
    std::unordered_map<NodeId, TimestampNs> ghosts_;
    std::set<std::pair<TimestampNs, NodeId>> expirations_;
    std::uint64_t hits_ = 0;
    std::uint64_t ghost_misses_ = 0;
    std::uint64_t total_ghost_misses_ = 0;
};

}  // namespace dwpdsim
