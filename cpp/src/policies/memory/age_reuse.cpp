#include "dwpdsim/policies/memory/age_reuse.hpp"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace dwpdsim {
namespace {

std::uint8_t log_bucket(std::uint64_t value, unsigned cap = 15) {
    return value ? static_cast<std::uint8_t>(std::min(cap,
        static_cast<unsigned>(64 - __builtin_clzll(value)))) : 0;
}

std::uint64_t seconds_ceil(std::uint64_t ns) {
    return ns / 1000000000ULL + (ns % 1000000000ULL != 0);
}

} // namespace

void AgeReuseHistory::record(std::uint64_t now) {
    if (count) { gap = log_bucket(seconds_ceil(now - last_ns)); }
    ++count;
    last_ns = now;
}

AgeReuseFeature AgeReuseFeature::from(const AgeReuseHistory& history,
    std::uint64_t now, std::uint8_t ancestor) {
    // Count zero has its own group: newly published checkpoints have not yet
    // received a logical demand. The eight positive-count groups match log2.
    const std::uint8_t count = history.count ? static_cast<std::uint8_t>(1 + std::min(7u,
        static_cast<unsigned>(63 - __builtin_clzll(history.count)))) : 0;
    return {count, log_bucket(seconds_ceil(now - history.last_ns)), history.gap, ancestor};
}

AgeReuseEstimator::AgeReuseEstimator(std::array<std::uint64_t, horizons_count> horizons_ns)
    : horizons_(horizons_ns), tables_(2 * horizons_count) {
    if (!horizons_[0] || !std::is_sorted(horizons_.begin(), horizons_.end()) ||
        std::adjacent_find(horizons_.begin(), horizons_.end()) != horizons_.end()) {
        throw std::invalid_argument("reuse horizons must be positive and strictly increasing");
    }
}

double AgeReuseEstimator::Cell::posterior(double parent, double strength) const {
    return (static_cast<double>(positives) + strength * parent) /
        (static_cast<double>(samples) + strength);
}

double AgeReuseEstimator::Table::predict(const AgeReuseFeature& f) const {
    const auto ca = f.count * ages + f.age;
    const auto cag = ca * gaps + f.gap;
    const auto caga = cag * ancestors + f.ancestor;
    const auto p0 = global.posterior(0.5, 2);
    const auto pa = age[f.age].posterior(p0, 32);
    const auto pc = count_age[ca].posterior(pa, 64);
    const auto pg = gap[cag].posterior(pc, 64);
    return ancestor[caga].posterior(pg, 256);
}

void AgeReuseEstimator::Table::add(const AgeReuseFeature& f, bool positive) {
    const auto ca = f.count * ages + f.age;
    const auto cag = ca * gaps + f.gap;
    global.add(positive);
    age[f.age].add(positive);
    count_age[ca].add(positive);
    gap[cag].add(positive);
    ancestor[cag * ancestors + f.ancestor].add(positive);
}

AgeReuseEstimator::Probabilities AgeReuseEstimator::predict(
    std::size_t kind, const AgeReuseFeature& feature) const {
    std::array<double, horizons_count> means{};
    std::array<std::size_t, horizons_count> weights{};
    std::size_t groups = 0;
    for (std::size_t h = 0; h < horizons_count; ++h) {
        means[groups] = tables_[kind * horizons_count + h].predict(feature);
        weights[groups] = 1;
        ++groups;
        while (groups > 1 && means[groups - 2] > means[groups - 1]) {
            const auto n = weights[groups - 2] + weights[groups - 1];
            means[groups - 2] = (means[groups - 2] * weights[groups - 2] +
                means[groups - 1] * weights[groups - 1]) / n;
            weights[groups - 2] = n;
            --groups;
        }
    }
    Probabilities result{};
    std::size_t offset = 0;
    for (std::size_t i = 0; i < groups; ++i) {
        for (std::size_t j = 0; j < weights[i]; ++j) { result[offset++] = means[i]; }
    }
    return result;
}

void AgeReuseEstimator::issue(std::size_t kind, std::uint64_t id,
    std::uint64_t logical_count, const AgeReuseFeature& feature, std::uint64_t now) {
    if (finished_) { throw std::logic_error("cannot issue reuse forecasts after finish"); }
    if (now > std::numeric_limits<std::uint64_t>::max() - horizons_.back()) {
        throw std::overflow_error("reuse forecast deadline overflows nanoseconds");
    }
    const auto prediction = predict(kind, feature);
    for (std::size_t h = 0; h < horizons_count; ++h) {
        pending_[kind][h].push_back({id, logical_count, now + horizons_[h], feature, prediction[h]});
    }
}

std::size_t AgeReuseEstimator::pending_forecasts() const {
    std::size_t result = 0;
    for (const auto& component : pending_) {
        for (const auto& queue : component) { result += queue.size(); }
    }
    return result;
}

std::size_t AgeReuseEstimator::shared_bytes() const { return tables_.capacity() * sizeof(Table); }
std::size_t AgeReuseEstimator::pending_payload_bytes() const { return pending_forecasts() * sizeof(Pending); }

} // namespace dwpdsim
