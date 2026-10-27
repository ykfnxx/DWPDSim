#pragma once

#include <array>
#include <map>
#include <random>
#include <unordered_map>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/hybrid_recovery.hpp"
#include "dwpdsim/policies/memory/age_reuse.hpp"
#include "dwpdsim/policies/storage/storage_policy.hpp"

namespace dwpdsim {

struct HybridCostObject { NodeId id; std::size_t kind; };
struct HybridCostWrite { HybridCostObject object; Placement placement; };
struct HybridCostCopyTimes {
    TimestampNs written, accessed;
    double ready_seconds;
};
struct HybridCostQuote {
    std::vector<HybridCostObject> victims;
    std::vector<HybridCostWrite> writes;
    std::vector<StorageActionIntent> capacity_actions;
    std::vector<HybridCopyChange> capacity_changes;
    std::vector<NodeId> protected_nodes;
    std::uint64_t released_bytes = 0;
    std::array<double, 2> nand_bytes{};
    std::array<double, 2> queue_until{};
    double drop = 0, benefit = 0, replacement = 0, media = 0, queue = 0, total = 0;
};

struct HybridCostCounters {
    std::uint64_t decisions = 0, quotes = 0, infeasible = 0;
    std::uint64_t scenario_evaluations = 0, logical_full = 0, logical_linear = 0;
    std::array<std::uint64_t, 2> selected_routes{};
    double drop = 0, benefit = 0, replacement = 0, media = 0, queue = 0, total = 0;
    double hazard_residual = 0;
    double expected_first_requests = 0;
    std::array<double, 2> predicted_nand_bytes{};
};

// Owns derived history and quotation statistics. Simulator alone commits copies,
// capacity actions and I/O. Every candidate in a decision uses the same demand law.
class JointMemoryCostPolicy {
  public:
    explicit JointMemoryCostPolicy(const SimulationConfig& config, const RadixTree& tree);
    void begin_request(const HybridPrefillRequest& request);
    void published(NodeId id, std::size_t kind, TimestampNs now);
    void end_request(TimestampNs now);
    void finish(TimestampNs observation_end);
    void io(NodeId id, std::size_t kind, Operation operation,
        StorageLocation location, std::uint64_t bytes, TimestampNs now, TraceReason reason,
        std::uint64_t sequence, std::optional<std::uint64_t> dependency);
    HybridCostQuote select(const std::vector<std::vector<HybridCostObject>>& candidates,
        HybridCostObject incoming, StoragePolicy& storage_policy,
        const StorageView& storage, TimestampNs now);
    const HybridCostCounters& counters() const { return counters_; }
    const AgeReuseEstimator& reuse() const { return reuse_; }
    const AgeReuseHistory& history(NodeId id, std::size_t kind) const;
    std::optional<HybridCostCopyTimes> copy_times(NodeId id, std::size_t kind) const;
    AgeReuseEstimator::Probabilities probabilities(NodeId id, std::size_t kind, TimestampNs now) const;
    std::size_t history_entries() const { return records_.size(); }

  private:
    struct Copy {
        StorageTier tier;
        std::uint32_t stream;
        TimestampNs written;
        TimestampNs touched;
        double lifetime_seconds;
        double ready_seconds = 0;
    };
    struct Record {
        std::array<AgeReuseHistory, 2> history;
        std::array<bool, 2> registered{};
        std::array<std::optional<Copy>, 2> copies;
        NodeId endpoint = 0;
        std::uint64_t terminal_count = 0;
    };
    struct FrontierRun { HybridCostObject object; double pages, lifetime; TimestampNs written; };
    struct Frontier { double pages = 0; std::vector<FrontierRun> runs; };
    struct Scenario {
        HybridPrefillRequest request;
        struct Event {
            double mass, time_seconds;
            std::size_t repaired_full;
            std::vector<NodeId> repaired_linear;
            unsigned sample;
        };
        std::vector<Event> events;
    };
    struct Value { double service = 0, wait = 0; };
    std::optional<HybridCostQuote> draft(const std::vector<HybridCostObject>& victims,
        std::optional<Placement> route, StoragePolicy& policy,
        const StorageView& storage, TimestampNs now) const;
    std::vector<Scenario> scenarios(const std::vector<HybridCostQuote>& quotes, TimestampNs now);
    std::vector<HybridCopyChange> changes(const HybridCostQuote& quote,
        bool writes, bool capacity) const;
    Value value(const std::vector<Scenario>& scenarios,
        const HybridRecoveryState& base, const std::vector<HybridCopyChange>& changes,
        const StoragePolicy& storage_policy,
        const StorageView& storage, TimestampNs now, const std::array<double, 2>& queue_until);
    void media_quote(HybridCostQuote& quote, TimestampNs now, const StorageView& storage);
    AgeReuseFeature feature(NodeId id, std::size_t kind, TimestampNs now) const;
    double lifetime(NodeId id, std::size_t kind, StorageTier tier, TimestampNs now) const;
    void append(Frontier& frontier, HybridCostObject object,
        double pages, double lifetime, TimestampNs now) const;

    const SimulationConfig& config_;
    const RadixTree& tree_;
    AgeReuseEstimator reuse_;
    HybridRecoveryModel recovery_;
    std::mt19937_64 random_;
    std::unordered_map<NodeId, Record> records_;
    std::array<std::vector<NodeId>, 2> known_;
    std::map<std::pair<StorageTier, std::uint32_t>, Frontier> frontiers_;
    std::array<double, 2> write_rate_{}, pending_write_bytes_{};
    std::array<double, 2> busy_until_{};
    std::unordered_map<std::uint64_t, double> io_completions_;
    std::optional<TimestampNs> io_time_;
    mutable std::unordered_map<NodeId, std::array<std::optional<AgeReuseEstimator::Probabilities>, 2>> prediction_cache_;
    mutable std::optional<TimestampNs> prediction_time_;
    std::optional<TimestampNs> rate_time_;
    std::uint64_t requests_ = 0;
    const HybridPrefillRequest* current_request_ = nullptr;
    HybridCostCounters counters_;
};

} // namespace dwpdsim
