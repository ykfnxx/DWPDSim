#include "dwpdsim/policies/adaptive_endurance_storage_policy.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace dwpdsim {
namespace {

constexpr double kNanosecondsPerSecond = 1'000'000'000.0;

double seconds(TimestampNs timestamp_ns) {
    return static_cast<double>(timestamp_ns) / kNanosecondsPerSecond;
}

double capacity_bytes(StorageTier tier, const StorageView& storage) {
    return static_cast<double>(
        storage.storage().tier(tier).capacity_blocks() * storage.block_size_bytes()
    );
}

double live_bytes(StorageTier tier, const StorageView& storage) {
    return static_cast<double>(
        storage.storage().tier(tier).used_blocks() * storage.block_size_bytes()
    );
}

}  // namespace

AdaptiveEnduranceStoragePolicy::GapEstimator::GapEstimator() {
    constexpr double start = 0.125;
    constexpr double finish = 86400.0;
    for (std::size_t index = 0; index < edges_.size(); ++index) {
        edges_[index] = start * std::pow(
            finish / start,
            static_cast<double>(index) / static_cast<double>(edges_.size() - 1)
        );
    }
    constexpr std::array<double, 9> prior{2, 5, 10, 15, 24, 40, 83, 150, 257.2775};
    constexpr std::array<double, 9> weights{300, 500, 1200, 1000, 900, 500, 350, 180, 70};
    for (std::size_t index = 0; index < prior.size(); ++index) {
        const auto edge = std::lower_bound(edges_.begin(), edges_.end(), prior[index]);
        const std::size_t bucket = std::min<std::size_t>(
            edges_.size() - 1,
            static_cast<std::size_t>(edge - edges_.begin())
        );
        counts_[bucket] += weights[index];
    }
}

void AdaptiveEnduranceStoragePolicy::GapEstimator::observe(double gap_seconds) {
    const auto edge = std::lower_bound(
        edges_.begin(),
        edges_.end(),
        std::max(0.125, gap_seconds)
    );
    const std::size_t bucket = std::min<std::size_t>(
        edges_.size() - 1,
        static_cast<std::size_t>(edge - edges_.begin())
    );
    counts_[bucket] += 1.0;
    ++samples_;
    if (samples_ % 2048 == 0) {
        recompute();
    }
    if (samples_ % 200'000 == 0) {
        for (double& count : counts_) {
            count *= 0.98;
        }
    }
}

double AdaptiveEnduranceStoragePolicy::GapEstimator::q95() const noexcept {
    return cached_q95_;
}

std::uint64_t AdaptiveEnduranceStoragePolicy::GapEstimator::samples() const noexcept {
    return samples_;
}

void AdaptiveEnduranceStoragePolicy::GapEstimator::recompute() {
    double total = 0.0;
    for (double count : counts_) {
        total += count;
    }
    const double target = total * 0.95;
    double accumulated = 0.0;
    for (std::size_t index = 0; index < counts_.size(); ++index) {
        accumulated += counts_[index];
        if (accumulated >= target) {
            cached_q95_ = edges_[index];
            return;
        }
    }
}

AdaptiveEnduranceStoragePolicy::AdaptiveEnduranceStoragePolicy(
    AdaptiveEndurancePolicyConfig config
)
    : config_(config) {}

BackgroundSchedule AdaptiveEnduranceStoragePolicy::background_schedule() const {
    return BackgroundSchedule{config_.background_period_ns};
}

void AdaptiveEnduranceStoragePolicy::on_request_begin(
    const RequestContext& request,
    const StorageView&
) {
    const AffinityId session = request.affinity_id == 0
                                   ? request.request_id
                                   : request.affinity_id;
    const auto previous = session_last_request_ns_.find(session);
    if (previous != session_last_request_ns_.end() &&
        request.timestamp_ns > previous->second) {
        gaps_.observe(seconds(request.timestamp_ns - previous->second));
    }
    session_last_request_ns_[session] = request.timestamp_ns;
}

DumpPlacementDecision AdaptiveEnduranceStoragePolicy::place_dump(
    const DumpContext& dump,
    const StorageView& storage
) const {
    const StorageTier tier = choose_tier(dump, storage);
    const AffinityId affinity = dump.request.affinity_id == 0
                                    ? dump.segment.segment_endpoint
                                    : dump.request.affinity_id;
    return DumpPlacementDecision{Placement{
        tier,
        stream_for(tier, affinity, dump.segment.segment_endpoint, storage),
    }};
}

std::uint64_t AdaptiveEnduranceStoragePolicy::capacity_limit_blocks(
    StorageTier tier,
    const StorageView& storage
) const {
    return static_cast<std::uint64_t>(
        static_cast<double>(storage.storage().tier(tier).capacity_blocks()) *
        config_.logical_fill_fraction
    );
}

std::optional<CapacityAction> AdaptiveEnduranceStoragePolicy::reclaim_for(
    const CapacityPressureContext& pressure,
    const StorageView& storage
) const {
    const auto victim = state_.lru_leaf(
        pressure.target_tier,
        pressure.protected_nodes,
        storage
    );
    if (!victim.has_value()) {
        return std::nullopt;
    }
    return TrimIntent{victim->endpoint, pressure.target_tier};
}

std::optional<MaintenanceAction> AdaptiveEnduranceStoragePolicy::next_background_action(
    const BackgroundTickContext& tick,
    const StorageView& storage
) const {
    const double now = seconds(tick.timestamp_ns);
    const double idle_threshold = idle_threshold_seconds(storage);
    std::optional<StoragePolicyState::SegmentTime> idle;
    StorageTier idle_tier = StorageTier::Slc;
    for (StorageTier tier : {StorageTier::Slc, StorageTier::Tlc}) {
        const auto candidate = state_.lru_leaf(tier, {}, storage);
        if (!candidate.has_value() ||
            now - seconds(candidate->timestamp_ns) < idle_threshold) {
            continue;
        }
        if (!idle.has_value() || candidate->timestamp_ns < idle->timestamp_ns ||
            (candidate->timestamp_ns == idle->timestamp_ns &&
             candidate->endpoint < idle->endpoint)) {
            idle = candidate;
            idle_tier = tier;
        }
    }

    const double promotion_age = effective_promotion_seconds(storage);
    auto promotion = state_.oldest_segment(StorageTier::Slc, storage);
    if (promotion.has_value() &&
        now - seconds(promotion->timestamp_ns) < promotion_age) {
        promotion.reset();
    }

    if (!idle.has_value() && !promotion.has_value()) {
        return std::nullopt;
    }

    const double idle_due = idle.has_value()
                                ? seconds(idle->timestamp_ns) + idle_threshold
                                : std::numeric_limits<double>::infinity();
    const double promotion_due = promotion.has_value()
                                     ? seconds(promotion->timestamp_ns) + promotion_age
                                     : std::numeric_limits<double>::infinity();
    if (idle.has_value() && idle_due <= promotion_due) {
        return TrimIntent{idle->endpoint, idle_tier};
    }

    const AffinityId affinity = promotion->endpoint;
    return RelocateIntent{
        promotion->endpoint,
        Placement{
            StorageTier::Tlc,
            stream_for(
                StorageTier::Tlc,
                affinity,
                promotion->endpoint,
                storage
            ),
        },
        RelocationCause::Background,
    };
}

std::optional<MaintenanceAction> AdaptiveEnduranceStoragePolicy::on_storage_access(
    const StorageAccessContext& access,
    const StorageView& storage
) const {
    if (access.source.tier != StorageTier::Slc) {
        return std::nullopt;
    }
    const NodeId endpoint = storage.tree().segment_leaf_for(access.access.node_id);
    const double age = seconds(access.access.request.timestamp_ns) -
                       seconds(state_.segment_first_ns(endpoint, StorageTier::Slc, storage));
    if (age < effective_promotion_seconds(storage)) {
        return std::nullopt;
    }
    const AffinityId affinity = access.access.request.affinity_id == 0
                                    ? endpoint
                                    : access.access.request.affinity_id;
    return RelocateIntent{
        endpoint,
        Placement{
            StorageTier::Tlc,
            stream_for(StorageTier::Tlc, affinity, endpoint, storage),
        },
        RelocationCause::Access,
    };
}

void AdaptiveEnduranceStoragePolicy::on_commit(
    const StorageMutation& mutation,
    const StorageView& storage_after_commit
) {
    state_.on_commit(mutation, storage_after_commit);
}

StoragePolicyStats AdaptiveEnduranceStoragePolicy::stats(const StorageView& storage) const {
    return StoragePolicyStats{
        state_.program_bytes(StorageTier::Slc),
        state_.program_bytes(StorageTier::Tlc),
        gaps_.samples(),
        gaps_.q95(),
        idle_threshold_seconds(storage),
    };
}

std::uint64_t AdaptiveEnduranceStoragePolicy::stable_hash(
    std::uint64_t value,
    std::uint64_t salt
) {
    std::uint64_t result = value + 0x9E3779B97F4A7C15ULL + salt;
    result = (result ^ (result >> 30)) * 0xBF58476D1CE4E5B9ULL;
    result = (result ^ (result >> 27)) * 0x94D049BB133111EBULL;
    return result ^ (result >> 31);
}

StorageTier AdaptiveEnduranceStoragePolicy::choose_tier(
    const DumpContext& dump,
    const StorageView& storage
) const {
    const double slc_program = static_cast<double>(
        state_.program_bytes(StorageTier::Slc)
    );
    const double tlc_program = static_cast<double>(
        state_.program_bytes(StorageTier::Tlc)
    );
    const double total_program = slc_program + tlc_program;
    if (total_program == 0.0) {
        return StorageTier::Slc;
    }
    const double slc_endurance = capacity_bytes(StorageTier::Slc, storage) *
                                 config_.slc_erase_budget;
    const double tlc_endurance = capacity_bytes(StorageTier::Tlc, storage) *
                                 config_.tlc_erase_budget;
    const double target_tlc = tlc_endurance / (slc_endurance + tlc_endurance);
    const double actual_tlc = tlc_program / total_program;
    const double deficit = std::max(0.0, target_tlc - actual_tlc);
    double probability = std::min(0.75, config_.direct_gain * deficit / target_tlc);

    const double slc_utilization = live_bytes(StorageTier::Slc, storage) /
                                   capacity_bytes(StorageTier::Slc, storage);
    const double tlc_utilization = live_bytes(StorageTier::Tlc, storage) /
                                   capacity_bytes(StorageTier::Tlc, storage);
    if (slc_utilization > 0.90 && tlc_utilization < 0.90 && actual_tlc < target_tlc) {
        double capacity_probability = std::min(0.50, (slc_utilization - 0.90) / 0.08);
        capacity_probability *= std::max(0.0, 1.0 - actual_tlc / target_tlc);
        probability = std::max(probability, capacity_probability);
    }

    const AffinityId affinity = dump.request.affinity_id == 0
                                    ? dump.segment.segment_endpoint
                                    : dump.request.affinity_id;
    constexpr std::uint64_t mask = (1ULL << 53) - 1;
    const double draw = static_cast<double>(stable_hash(affinity, 47) & mask) /
                        static_cast<double>(1ULL << 53);
    return draw < probability ? StorageTier::Tlc : StorageTier::Slc;
}

std::uint32_t AdaptiveEnduranceStoragePolicy::stream_for(
    StorageTier tier,
    AffinityId affinity_id,
    NodeId segment_endpoint,
    const StorageView& storage
) const {
    const std::uint64_t key = affinity_id == 0 ? segment_endpoint : affinity_id;
    return static_cast<std::uint32_t>(
        stable_hash(key, 29) % storage.storage().tier(tier).stream_count()
    );
}

double AdaptiveEnduranceStoragePolicy::idle_threshold_seconds(
    const StorageView& storage
) const {
    double threshold = std::clamp(
        config_.idle_multiplier * gaps_.q95(),
        60.0,
        6.0 * 3600.0
    );
    const double slc_utilization = live_bytes(StorageTier::Slc, storage) /
                                   capacity_bytes(StorageTier::Slc, storage);
    if (slc_utilization > config_.slc_soft_utilization) {
        threshold *= std::max(
            0.02,
            std::exp(
                -config_.occupancy_decay *
                (slc_utilization - config_.slc_soft_utilization)
            )
        );
    }
    return std::max(60.0, threshold);
}

double AdaptiveEnduranceStoragePolicy::effective_promotion_seconds(
    const StorageView& storage
) const {
    const double slc_pressure = static_cast<double>(
        state_.program_bytes(StorageTier::Slc)
    ) / (capacity_bytes(StorageTier::Slc, storage) * config_.slc_erase_budget);
    const double tlc_pressure = static_cast<double>(
        state_.program_bytes(StorageTier::Tlc)
    ) / (capacity_bytes(StorageTier::Tlc, storage) * config_.tlc_erase_budget);
    const double ratio = (tlc_pressure + 1e-12) / (slc_pressure + 1e-12);
    const double factor = std::clamp(
        std::pow(ratio, config_.adaptation_gain),
        0.5,
        4.0
    );
    return config_.promotion_seconds * factor;
}

}  // namespace dwpdsim
