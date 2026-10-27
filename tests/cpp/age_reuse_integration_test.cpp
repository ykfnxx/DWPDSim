#include <cassert>
#include <memory>
#include <unordered_map>

#include "dwpdsim/policies/memory/age_reuse.hpp"

using namespace dwpdsim;
constexpr std::uint64_t second = 1000000000ULL;

int main() {
    auto model = std::make_unique<AgeReuseEstimator>(
        std::array<std::uint64_t, 3>{2 * second, 4 * second, 8 * second});
    std::unordered_map<std::uint64_t, AgeReuseHistory> histories;
    const auto count = [&](std::size_t, std::uint64_t id) { return histories.at(id).count; };

    // Identical counts/gaps, different current idle ages. Logical observations
    // continue without any residency or hit condition.
    for (std::uint64_t episode = 0; episode < 128; ++episode) {
        const auto now = (200 + 10 * episode) * second;
        model->settle_before(now, count);
        const auto recent = 2 * episode, old = recent + 1;
        histories[recent] = {3, now, 16};
        histories[old] = {3, now - 100 * second, 16};
        model->issue(0, recent, histories.at(recent).count,
            AgeReuseFeature::from(histories.at(recent), now, 0), now);
        model->issue(0, old, histories.at(old).count,
            AgeReuseFeature::from(histories.at(old), now, 0), now);
        model->settle_before(now + second, count);
        histories.at(recent).record(now + second);
        // An early positive is still pending at the complete horizon deadline.
        assert(model->scores(0, 0).samples == 2 * episode);
    }
    const auto now = 1600 * second;
    model->settle_before(now, count);
    const AgeReuseHistory recent{3, now, 16}, old{3, now - 100 * second, 16};
    const auto fresh = model->predict(0, AgeReuseFeature::from(recent, now, 0));
    const auto stale = model->predict(0, AgeReuseFeature::from(old, now, 0));
    for (std::size_t h = 0; h < 3; ++h) {
        assert(model->scores(0, h).samples == 256);
        assert(model->scores(0, h).positives == 128);
        assert(fresh[h] > stale[h]);
        assert(model->predict(1, AgeReuseFeature::from(recent, now, 0))[h] == 0.5);
        if (h) { assert(fresh[h - 1] <= fresh[h]); assert(stale[h - 1] <= stale[h]); }
    }

    // A later request at the exact forecast timestamp is a valid zero-gap
    // positive. A request on the horizon boundary also belongs to that horizon.
    histories[1000] = {1, now, 16};
    model->issue(0, 1000, 1, AgeReuseFeature::from(histories.at(1000), now, 0), now);
    histories.at(1000).record(now);
    model->settle_before(now + 2 * second, count);
    assert(model->scores(0, 0).samples == 256);
    model->settle_before(now + 2 * second + 1, count);
    assert(model->scores(0, 0).samples == 257);
    assert(model->scores(0, 0).positives == 129);

    histories[1001] = {1, now + 3 * second, 16};
    model->issue(1, 1001, 1,
        AgeReuseFeature::from(histories.at(1001), now + 3 * second, 0), now + 3 * second);
    model->settle_before(now + 5 * second, count);
    histories.at(1001).record(now + 5 * second);
    model->settle_before(now + 5 * second + 1, count);
    assert(model->scores(1, 0).samples == 1);
    assert(model->scores(1, 0).positives == 1);

    // No demand during a fully observed window is negative. Unfinished tail
    // windows are censored even when a positive was observed early.
    model->settle_before(now + 20 * second, count);
    const auto mature = model->scores(0, 0).samples;
    histories[1002] = {1, now + 20 * second, 16};
    model->issue(0, 1002, 1,
        AgeReuseFeature::from(histories.at(1002), now + 20 * second, 0), now + 20 * second);
    histories.at(1002).record(now + 21 * second);
    model->finish(now + 21 * second, count);
    assert(model->pending_forecasts() == 0);
    for (std::size_t h = 0; h < 3; ++h) { assert(model->scores(0, h).censored == 1); }
    assert(model->scores(0, 0).samples == mature);
}
