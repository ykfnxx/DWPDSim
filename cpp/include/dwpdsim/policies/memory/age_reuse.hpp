#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <vector>

namespace dwpdsim {

// Logical applicability, including misses, is separate from consumed cache hits.
struct AgeReuseHistory {
    std::uint64_t count = 0;
    // Before the first demand this is the first publication's cold-start anchor.
    std::uint64_t last_ns = 0;
    std::uint8_t gap = 16;
    void record(std::uint64_t now);
};

struct AgeReuseFeature {
    std::uint8_t count = 0, age = 0, gap = 16, ancestor = 0;
    static AgeReuseFeature from(const AgeReuseHistory& history, std::uint64_t now,
        std::uint8_t ancestor);
};

struct AgeReuseScores {
    std::uint64_t samples = 0, positives = 0, censored = 0;
    double brier_sum = 0;
};

// Online landmark forecasts: all labels mature at their deadlines, including
// early positives. FULL and LINEAR use independent tables with the same features.
class AgeReuseEstimator {
  public:
    static constexpr std::size_t horizons_count = 3;
    using Probabilities = std::array<double, horizons_count>;
    explicit AgeReuseEstimator(std::array<std::uint64_t, horizons_count> horizons_ns);

    Probabilities predict(std::size_t kind, const AgeReuseFeature& feature) const;
    void issue(std::size_t kind, std::uint64_t id, std::uint64_t logical_count,
        const AgeReuseFeature& feature, std::uint64_t now);

    // Call before recording a request at now. Equal-time requests remain visible
    // to forecasts whose deadline equals now, until the clock advances.
    template<class CountReader>
    void settle_before(std::uint64_t now, const CountReader& read_count) {
        settle(now, false, read_count);
    }

    template<class CountReader>
    void finish(std::uint64_t observation_end, const CountReader& read_count) {
        settle(observation_end, true, read_count);
        for (std::size_t kind = 0; kind < 2; ++kind) {
            for (std::size_t h = 0; h < horizons_count; ++h) {
                auto& queue = pending_[kind][h];
                scores_[kind][h].censored += queue.size();
                queue.clear();
            }
        }
        finished_ = true;
    }

    const std::array<std::uint64_t, horizons_count>& horizons_ns() const { return horizons_; }
    const AgeReuseScores& scores(std::size_t kind, std::size_t h) const { return scores_[kind][h]; }
    std::size_t pending_forecasts() const;
    std::size_t shared_bytes() const;
    std::size_t pending_payload_bytes() const;

  private:
    static constexpr std::size_t counts = 9, ages = 16, gaps = 17, ancestors = 8;
    struct Cell {
        std::uint64_t samples = 0, positives = 0;
        void add(bool positive) { ++samples; positives += positive; }
        double posterior(double parent, double strength) const;
    };
    struct Table {
        Cell global;
        std::array<Cell, ages> age;
        std::array<Cell, counts * ages> count_age;
        std::array<Cell, counts * ages * gaps> gap;
        std::array<Cell, counts * ages * gaps * ancestors> ancestor;
        double predict(const AgeReuseFeature& feature) const;
        void add(const AgeReuseFeature& feature, bool positive);
    };
    struct Pending {
        std::uint64_t id, count, deadline;
        AgeReuseFeature feature;
        double prediction;
    };

    template<class CountReader>
    void settle(std::uint64_t now, bool include_now, const CountReader& read_count) {
        for (std::size_t kind = 0; kind < 2; ++kind) {
            for (std::size_t h = 0; h < horizons_count; ++h) {
                auto& queue = pending_[kind][h];
                while (!queue.empty() && (queue.front().deadline < now ||
                        (include_now && queue.front().deadline == now))) {
                    const auto& query = queue.front();
                    const bool positive = read_count(kind, query.id) > query.count;
                    tables_[kind * horizons_count + h].add(query.feature, positive);
                    auto& score = scores_[kind][h];
                    ++score.samples;
                    score.positives += positive;
                    const double error = query.prediction - static_cast<double>(positive);
                    score.brier_sum += error * error;
                    queue.pop_front();
                }
            }
        }
    }

    std::array<std::uint64_t, horizons_count> horizons_;
    std::vector<Table> tables_;
    std::array<std::array<std::deque<Pending>, horizons_count>, 2> pending_;
    std::array<std::array<AgeReuseScores, horizons_count>, 2> scores_{};
    bool finished_ = false;
};

} // namespace dwpdsim
