#include "dwpdsim/policies/memory/joint_cost.hpp"

#include <algorithm>
#include <cmath>
#include <functional>
#include <limits>
#include <set>
#include <stdexcept>
#include <type_traits>

namespace dwpdsim {
namespace {

std::array<std::uint64_t, 3> horizons(const JointCostConfig& c) {
    std::array<std::uint64_t, 3> result{};
    for (std::size_t i = 0; i < result.size(); ++i) {
        if (!std::isfinite(c.horizons_seconds[i]) || c.horizons_seconds[i] <= 0 ||
            c.horizons_seconds[i] >= 1e9) {
            throw std::invalid_argument("cost horizons must be finite positive seconds below 1e9");
        }
        result[i] = static_cast<std::uint64_t>(std::llround(c.horizons_seconds[i] * 1e9));
    }
    return result;
}

StorageTier other(StorageTier t) { return t == StorageTier::Slc ? StorageTier::Tlc : StorageTier::Slc; }
std::uint64_t units(std::uint64_t bytes, std::uint64_t unit) { return bytes / unit + (bytes % unit != 0); }

} // namespace

JointMemoryCostPolicy::JointMemoryCostPolicy(const SimulationConfig& config, const RadixTree& tree)
    : config_(config), tree_(tree), reuse_(horizons(config.hybrid_attention.cost)),
      recovery_(config.hybrid_attention.tokens_per_block, config.block_size_bytes,
          config.hybrid_attention.cost.storage_read_bytes_per_second,
          config.hybrid_attention.cost.gpu_bytes_per_second,
          config.hybrid_attention.cost.prefill_tokens_per_second),
      random_(config.hybrid_attention.cost.seed) {
    const auto& c = config.hybrid_attention.cost;
    if (!c.candidates_per_kind || !c.scenario_limit || !c.sample_every || !c.samples ||
        c.reclaim_bytes > config.memory.capacity_bytes ||
        !c.nand_page_bytes || !c.nand_pages_per_block ||
        config.block_size_bytes % c.nand_page_bytes ||
        !std::isfinite(c.overprovisioning) || c.overprovisioning <= 0 || c.overprovisioning >= 1 ||
        !std::isfinite(c.wear_weight) || c.wear_weight < 0) {
        throw std::invalid_argument("invalid joint cost search, sampling, NAND geometry or wear parameters");
    }
    for (std::size_t k = 0; k < 2; ++k) {
        if (!std::isfinite(c.nand_seconds_per_gib[k]) || c.nand_seconds_per_gib[k] < 0 ||
            !std::isfinite(c.storage_write_bytes_per_second[k]) || c.storage_write_bytes_per_second[k] <= 0) {
            throw std::invalid_argument("cost media prices must be nonnegative and write bandwidths positive");
        }
    }
}

const AgeReuseHistory& JointMemoryCostPolicy::history(NodeId id, std::size_t kind) const {
    return records_.at(id).history[kind];
}

AgeReuseFeature JointMemoryCostPolicy::feature(NodeId id, std::size_t kind, TimestampNs now) const {
    const auto& h = history(id, kind);
    bool newer = false;
    unsigned ratio = 3;
    auto parent = tree_.parent(id);
    for (unsigned steps = 0; parent && steps < 128; ++steps, parent = tree_.parent(*parent)) {
        const auto& ancestor = records_.at(*parent).history[kind];
        if (!ancestor.count) { continue; }
        newer |= ancestor.last_ns > h.last_ns;
        const auto r = h.count * 8 < ancestor.count ? 0U :
            h.count * 4 < ancestor.count ? 1U : h.count * 2 < ancestor.count ? 2U : 3U;
        ratio = std::min(ratio, r);
    }
    return AgeReuseFeature::from(h, now, static_cast<std::uint8_t>(newer * 4 + ratio));
}

AgeReuseEstimator::Probabilities JointMemoryCostPolicy::probabilities(
    NodeId id, std::size_t kind, TimestampNs now) const {
    if (!prediction_time_ || *prediction_time_ != now) { prediction_cache_.clear(); prediction_time_ = now; }
    auto& cached = prediction_cache_[id][kind];
    if (!cached) { cached = reuse_.predict(kind, feature(id, kind, now)); }
    return *cached;
}

void JointMemoryCostPolicy::begin_request(const HybridPrefillRequest& q) {
    current_request_ = &q;
    reuse_.settle_before(q.timestamp_ns, [&](std::size_t k, NodeId id) { return history(id, k).count; });
    if (rate_time_ && q.timestamp_ns > *rate_time_) {
        const auto elapsed = (q.timestamp_ns - *rate_time_) / 1e9;
        const auto decay = std::exp(-elapsed / 300);
        for (std::size_t k = 0; k < 2; ++k) {
            write_rate_[k] = write_rate_[k] * decay + pending_write_bytes_[k] / elapsed * (1 - decay);
            pending_write_bytes_[k] = 0;
        }
    }
    if (!rate_time_ || q.timestamp_ns > *rate_time_) { rate_time_ = q.timestamp_ns; }
    const auto tokens = config_.hybrid_attention.tokens_per_block;
    const auto input = q.input_tokens ? q.input_tokens : q.hash_ids.size() * tokens;
    const auto limit = std::min(input - 1, q.max_prefix_tokens.value_or(std::numeric_limits<std::uint64_t>::max()));
    for (std::size_t i = 0; i < q.hash_ids.size(); ++i) {
        const auto id = q.hash_ids[i];
        auto& r = records_[id];
        r.endpoint = q.hash_ids.back();
        if (!r.registered[0]) { r.registered[0] = true; known_[0].push_back(id); }
        r.history[0].record(q.timestamp_ns);
        ++counters_.logical_full;
        const auto& node = tree_.node(id);
        if ((i + 1) * tokens <= limit && node.hybrid && node.hybrid->components[1].published) {
            r.history[1].record(q.timestamp_ns);
            ++counters_.logical_linear;
        }
    }
    ++requests_;
    ++records_.at(q.hash_ids.back()).terminal_count;
    prediction_cache_.clear();
}

void JointMemoryCostPolicy::published(NodeId id, std::size_t kind, TimestampNs now) {
    auto& r = records_.at(id);
    if (!r.registered[kind]) {
        r.registered[kind] = true;
        r.history[kind].last_ns = now;
        known_[kind].push_back(id);
        prediction_cache_.erase(id);
    }
}

void JointMemoryCostPolicy::end_request(TimestampNs now) {
    current_request_ = nullptr;
    const auto& c = config_.hybrid_attention.cost;
    if (requests_ % c.sample_every) { return; }
    for (std::size_t kind = 0; kind < 2; ++kind) {
        if (known_[kind].empty()) { continue; }
        std::uniform_int_distribution<std::size_t> choose(0, known_[kind].size() - 1);
        for (std::size_t i = 0; i < c.samples; ++i) {
            const auto id = known_[kind][choose(random_)];
            reuse_.issue(kind, id, history(id, kind).count, feature(id, kind, now), now);
        }
    }
}

void JointMemoryCostPolicy::finish(TimestampNs observation_end) {
    reuse_.finish(observation_end, [&](std::size_t k, NodeId id) { return history(id, k).count; });
}

double JointMemoryCostPolicy::lifetime(NodeId id, std::size_t kind, StorageTier tier, TimestampNs now) const {
    const auto k = storage_tier_index(tier);
    if (write_rate_[k] == 0) { return std::numeric_limits<double>::infinity(); }
    const auto capacity = tier == StorageTier::Slc ? config_.slc.capacity_bytes : config_.tlc.capacity_bytes;
    const auto base = capacity / write_rate_[k];
    const auto p = probabilities(id, kind, now).back();
    const auto rate = -std::log1p(-p) / config_.hybrid_attention.cost.horizons_seconds.back();
    // One expected SSD return may refresh storage recency, then the copy is
    // promoted to DRAM. Logical DRAM hits do not repeatedly refresh SSD lifetime.
    const auto x = rate * base;
    const auto extension = x > 1e-4 ? -std::expm1(-x) / rate - base * std::exp(-x) :
        base * (x / 2 - x * x / 3 + x * x * x / 8);
    return base + extension;
}

void JointMemoryCostPolicy::append(Frontier& f, HybridCostObject object,
    double pages, double life, TimestampNs now) const {
    const double block = config_.hybrid_attention.cost.nand_pages_per_block;
    if (f.pages && pages >= block - f.pages) {
        pages -= block - f.pages;
        f = {};
    }
    if (pages >= block) { pages = std::fmod(pages, block); }
    if (pages > 0) { f.runs.push_back({object, pages, life, now}); f.pages += pages; }
}

void JointMemoryCostPolicy::io(NodeId id, std::size_t kind, Operation op,
    StorageLocation location, std::uint64_t bytes, TimestampNs now, TraceReason reason) {
    auto& copy = records_.at(id).copies[kind];
    if (op == Operation::Write) {
        const auto life = lifetime(id, kind, location.tier, now);
        copy = Copy{location.tier, location.stream_id, now, now, life};
        pending_write_bytes_[storage_tier_index(location.tier)] += bytes;
        append(frontiers_[{location.tier, location.stream_id}], {id, kind},
            static_cast<double>(bytes) / config_.hybrid_attention.cost.nand_page_bytes, life, now);
    } else if (op == Operation::Read && reason == TraceReason::StorageHit) {
        copy.value().touched = now;
    } else if (op == Operation::Trim && copy && copy->tier == location.tier &&
        copy->stream == location.stream_id) { copy.reset(); }
}

std::optional<HybridCostQuote> JointMemoryCostPolicy::draft(
    const std::vector<HybridCostObject>& victims, std::optional<Placement> route,
    StoragePolicy& policy, const StorageView& storage, TimestampNs now) const {
    HybridCostQuote quote;
    quote.victims = victims;
    for (const auto& v : victims) {
        const auto& node = tree_.node(v.id);
        const auto& c = node.hybrid->components[v.kind];
        quote.released_bytes += c.size_bytes;
        if (route && !c.on_storage()) {
            quote.writes.push_back({v, node.on_storage ?
                Placement{node.storage_tier, node.storage_stream_id} : *route});
            quote.protected_nodes.push_back(v.id);
        }
    }
    std::array<std::uint64_t, 2> used{
        storage.storage().tier(StorageTier::Slc).used_blocks(),
        storage.storage().tier(StorageTier::Tlc).used_blocks()};
    std::function<bool(StorageTier, std::uint64_t)> reserve = [&](StorageTier tier, std::uint64_t n) {
        const auto k = storage_tier_index(tier);
        const auto limit = policy.capacity_limit_blocks(tier, storage);
        if (n > limit) { return false; }
        while (used[k] + n > limit) {
            const auto action = policy.reclaim_for(CapacityPressureContext{now,
                CapacityCause::DumpAdmission, tier, n,
                NodeSpan{quote.protected_nodes.data(), quote.protected_nodes.size()}}, storage);
            if (!action) { return false; }
            const bool feasible = std::visit([&](const auto& intent) {
                using T = std::decay_t<decltype(intent)>;
                const auto endpoint = [&]() {
                    if constexpr (std::is_same_v<T, TrimIntent>) { return intent.segment_endpoint; }
                    else { return intent.source_segment_endpoint; }
                }();
                const auto nodes = storage.resident_nodes(endpoint, tier);
                const auto freed = storage.resident_blocks(endpoint, tier);
                quote.protected_nodes.insert(quote.protected_nodes.end(), nodes.begin(), nodes.end());
                if constexpr (std::is_same_v<T, RelocateIntent>) {
                    if (!reserve(intent.destination.tier, freed)) { return false; }
                    used[storage_tier_index(intent.destination.tier)] += freed;
                }
                used[k] -= freed;
                quote.capacity_actions.push_back(intent);
                for (const auto id : nodes) {
                    for (std::size_t component = 0; component < 2; ++component) {
                        if (!tree_.node(id).hybrid->components[component].on_storage()) { continue; }
                        std::optional<StorageTier> destination;
                        if constexpr (std::is_same_v<T, RelocateIntent>) { destination = intent.destination.tier; }
                        quote.capacity_changes.push_back({id, component, false, true, destination});
                    }
                }
                return true;
            }, *action);
            if (!feasible) { return false; }
        }
        return true;
    };
    for (const auto& write : quote.writes) {
        const auto n = units(tree_.node(write.object.id).hybrid->components[write.object.kind].size_bytes,
            config_.block_size_bytes);
        if (!reserve(write.placement.tier, n)) { return std::nullopt; }
        used[storage_tier_index(write.placement.tier)] += n;
    }
    // Search exclusions include earlier planned victims; execution protection
    // contains only incoming writes, otherwise a quoted trim protects itself.
    quote.protected_nodes.clear();
    for (const auto& write : quote.writes) { quote.protected_nodes.push_back(write.object.id); }
    return quote;
}

std::vector<HybridCopyChange> JointMemoryCostPolicy::changes(
    const HybridCostQuote& quote, bool writes, bool capacity) const {
    std::vector<HybridCopyChange> result;
    for (const auto& v : quote.victims) { result.push_back({v.id, v.kind, true, false, {}}); }
    if (capacity) {
        result.insert(result.end(), quote.capacity_changes.begin(), quote.capacity_changes.end());
    }
    if (writes) {
        for (const auto& write : quote.writes) {
            result.push_back({write.object.id, write.object.kind, false, false, write.placement.tier});
        }
    }
    return result;
}

std::vector<JointMemoryCostPolicy::Scenario> JointMemoryCostPolicy::scenarios(
    const std::vector<HybridCostQuote>& quotes, TimestampNs now) {
    std::set<std::pair<NodeId, std::size_t>> anchors;
    for (const auto& quote : quotes) {
        for (const auto& v : quote.victims) { anchors.emplace(v.id, v.kind); }
        for (const auto& v : quote.capacity_changes) { anchors.emplace(v.id, v.kind); }
    }
    std::map<NodeId, double> endpoints;
    std::vector<HybridCostObject> objects;
    std::vector<AgeReuseEstimator::Probabilities> p;
    for (const auto& [id, kind] : anchors) {
        objects.push_back({id, kind});
        p.push_back(probabilities(id, kind, now));
        auto& quality = endpoints[records_.at(id).endpoint];
        quality = std::max(quality, p.back().back());
        if (kind == 1) { endpoints[id] = std::max(endpoints[id], p.back().back()); }
    }
    std::vector<std::pair<double, NodeId>> ranked;
    for (const auto& [id, quality] : endpoints) { ranked.emplace_back(-quality, id); }
    std::sort(ranked.begin(), ranked.end());
    if (ranked.size() > config_.hybrid_attention.cost.scenario_limit) {
        ranked.resize(config_.hybrid_attention.cost.scenario_limit);
    }
    std::vector<Scenario> result;
    std::vector<std::vector<double>> covers;
    std::vector<double> prior;
    double prior_total = 0;
    for (const auto& [quality, endpoint] : ranked) {
        static_cast<void>(quality);
        Scenario scene;
        for (auto at = std::optional<NodeId>(endpoint); at; at = tree_.parent(*at)) {
            scene.request.hash_ids.push_back(*at);
        }
        std::reverse(scene.request.hash_ids.begin(), scene.request.hash_ids.end());
        // A hypothetical next prompt continues one uncached token beyond an
        // observed endpoint. It publishes no synthetic cache/decode identity.
        scene.request.input_tokens = scene.request.hash_ids.size() * config_.hybrid_attention.tokens_per_block + 1;
        std::set<NodeId> path(scene.request.hash_ids.begin(), scene.request.hash_ids.end());
        std::vector<double> column;
        for (const auto& object : objects) { column.push_back(path.count(object.id) ? 1 : 0); }
        covers.push_back(std::move(column));
        result.push_back(std::move(scene));
        prior.push_back(records_.at(endpoint).terminal_count + 0.25);
        prior_total += prior.back();
    }
    double survival = 1, left = 0;
    for (std::size_t h = 0; h < 3; ++h) {
        std::vector<double> target(objects.size()), fitted(objects.size()), intensity(result.size());
        double target_mean = 0;
        for (std::size_t i = 0; i < objects.size(); ++i) {
            target[i] = -std::log1p(-p[i][h]) + (h ? std::log1p(-p[i][h - 1]) : 0);
            target_mean += target[i] / objects.size();
        }
        // Nonnegative path intensities fit object cumulative-hazard increments.
        // A path generates all its prefix demands together; marginals are not
        // multiplied into an assumed FULL/LINEAR joint hit probability.
        for (unsigned iteration = 0; iteration < 12; ++iteration) {
            for (std::size_t j = 0; j < result.size(); ++j) {
                double gradient = intensity[j] - target_mean * prior[j] / prior_total, norm = 1;
                for (std::size_t i = 0; i < objects.size(); ++i) {
                    gradient += covers[j][i] * (fitted[i] - target[i]);
                    norm += covers[j][i];
                }
                const auto next = std::max(0.0, intensity[j] - gradient / norm);
                for (std::size_t i = 0; i < objects.size(); ++i) {
                    fitted[i] += covers[j][i] * (next - intensity[j]);
                }
                intensity[j] = next;
            }
        }
        double sum = 0;
        for (const auto rate : intensity) { sum += rate; }
        for (std::size_t i = 0; i < objects.size(); ++i) {
            const auto error = fitted[i] - target[i];
            counters_.hazard_residual += error * error;
        }
        const double right = config_.hybrid_attention.cost.horizons_seconds[h];
        if (sum > 0) {
            const auto within = sum > 1e-4 ? 1 / sum - 1 / std::expm1(sum) :
                0.5 - sum / 12 + sum * sum * sum / 720;
            for (std::size_t j = 0; j < result.size(); ++j) {
                result[j].mass[h] = survival * -std::expm1(-sum) * intensity[j] / sum;
                result[j].time_seconds[h] = left + (right - left) * within;
            }
        }
        survival *= std::exp(-sum);
        left = right;
    }
    return result;
}

double JointMemoryCostPolicy::value(const std::vector<Scenario>& scenes,
    const std::vector<HybridCopyChange>& changes, const StoragePolicy& policy,
    const StorageView& storage, TimestampNs now) {
    double result = 0;
    const HybridRecoveryState projected(tree_, changes);
    const auto stats = policy.stats(storage);
    const auto background = policy.background_schedule().period_ns / 1e9;
    for (const auto& scene : scenes) {
        for (std::size_t h = 0; h < 3; ++h) {
            if (scene.mass[h] == 0) { continue; }
            const auto time = scene.time_seconds[h];
            for (unsigned sample = 0; sample < 3; ++sample) {
                auto at_time = changes;
                for (const auto id : scene.request.hash_ids) {
                    for (std::size_t kind = 0; kind < 2; ++kind) {
                        const auto copy = projected.component(id, kind);
                        if (!copy.storage || copy.in_dram) { continue; }
                        const auto& actual = tree_.node(id).hybrid->components[kind];
                        const bool added = !actual.on_storage() || tree_.node(id).storage_tier != *copy.storage;
                        const auto touched = added ? now : records_.at(id).copies[kind].value().touched;
                        auto chance = std::exp(-time / lifetime(id, kind, *copy.storage, now));
                        if (background > 0 && stats.idle_threshold_seconds > 0) {
                            const auto eligibility = touched / 1e9 + stats.idle_threshold_seconds;
                            const auto tick = std::ceil(eligibility / background) * background;
                            if (now / 1e9 + time >= tick) { chance = 0; }
                        }
                        if (added && time < units(copy.size_bytes, config_.block_size_bytes) *
                            config_.block_size_bytes / config_.hybrid_attention.cost.storage_write_bytes_per_second[
                                storage_tier_index(*copy.storage)]) { chance = 0; }
                        // Common stratified survival draws across every action.
                        const auto shift = static_cast<double>((id * 11400714819323198485ULL) >> 11) /
                            9007199254740992.0;
                        const auto u = std::fmod((sample + 0.5) / 3 + shift, 1.0);
                        if (u >= chance) { at_time.push_back({id, kind, false, false, {}, false, true}); }
                    }
                }
                result += scene.mass[h] * recovery_.evaluate(scene.request,
                    HybridRecoveryState(tree_, at_time)).total_seconds / 3;
                ++counters_.scenario_evaluations;
            }
        }
    }
    return result;
}

void JointMemoryCostPolicy::media_quote(HybridCostQuote& quote, TimestampNs now,
    const StorageView& storage) {
    const auto& c = config_.hybrid_attention.cost;
    auto frontiers = frontiers_; // Bounded partial-block summaries, never a tree/FTL snapshot.
    std::set<std::tuple<NodeId, std::size_t, StorageTier>> invalidated;
    std::map<std::pair<NodeId, std::size_t>, Copy> virtual_copies;
    const auto gc_ratio = [&](const Frontier& f, StorageTier tier) {
        const auto k = storage_tier_index(tier);
        const auto capacity = tier == StorageTier::Slc ? config_.slc.capacity_bytes : config_.tlc.capacity_bytes;
        const auto gc_time = write_rate_[k] > 0 ? capacity * c.overprovisioning / write_rate_[k] :
            std::numeric_limits<double>::infinity();
        double live = 0;
        for (const auto& run : f.runs) {
            if (invalidated.count({run.object.id, run.object.kind, tier})) { continue; }
            const auto projected = virtual_copies.find({run.object.id, run.object.kind});
            const auto existing = projected == virtual_copies.end() ?
                records_.at(run.object.id).copies[run.object.kind] : std::optional<Copy>(projected->second);
            if (existing && existing->written == run.written && existing->tier == tier) {
                const auto life = lifetime(run.object.id, run.object.kind, tier, now);
                live += run.pages * (std::isinf(gc_time) ? 0 : std::exp(-gc_time / life));
            }
        }
        const auto valid = f.pages ? live / f.pages : 0;
        return valid / (1 - valid + c.overprovisioning);
    };
    const auto invalidate = [&](NodeId id, std::size_t kind) {
        const auto& copy = records_.at(id).copies[kind].value();
        auto& f = frontiers[{copy.tier, copy.stream}];
        const auto before = gc_ratio(f, copy.tier);
        invalidated.emplace(id, kind, copy.tier);
        quote.nand_bytes[storage_tier_index(copy.tier)] +=
            f.pages * (gc_ratio(f, copy.tier) - before) * c.nand_page_bytes;
    };
    const auto program = [&](HybridCostObject object, Placement placement) {
        const auto bytes = units(tree_.node(object.id).hybrid->components[object.kind].size_bytes,
            config_.block_size_bytes) * config_.block_size_bytes;
        const auto pages = static_cast<double>(bytes) / c.nand_page_bytes;
        const auto k = storage_tier_index(placement.tier);
        auto& f = frontiers[{placement.tier, placement.stream_id}];
        const auto before = gc_ratio(f, placement.tier);
        const auto capacity = placement.tier == StorageTier::Slc ? config_.slc.capacity_bytes : config_.tlc.capacity_bytes;
        const auto gc_time = write_rate_[k] > 0 ? capacity * c.overprovisioning / write_rate_[k] :
            std::numeric_limits<double>::infinity();
        const auto life = lifetime(object.id, object.kind, placement.tier, now);
        const auto alive = std::isinf(gc_time) ? 0 : std::exp(-gc_time / life);
        const auto own = alive / (1 - alive + c.overprovisioning);
        const auto first = std::min(pages, c.nand_pages_per_block - f.pages);
        const auto mixed_live = (f.pages * before * (1 + c.overprovisioning) / (1 + before) +
            first * alive) / (f.pages + first);
        const auto mixed = mixed_live / (1 - mixed_live + c.overprovisioning);
        const auto gc = pages * own + f.pages * (mixed - before) + first * (mixed - own);
        // One amortized mapping page per data block is an explicit model term.
        quote.nand_bytes[k] += bytes + gc * c.nand_page_bytes + bytes / c.nand_pages_per_block;
        quote.queue += bytes / c.storage_write_bytes_per_second[k];
        append(f, object, pages, life, now);
        virtual_copies.insert_or_assign({object.id, object.kind},
            Copy{placement.tier, placement.stream_id, now, now, life});
    };
    for (const auto& action : quote.capacity_actions) {
        std::visit([&](const auto& intent) {
            using T = std::decay_t<decltype(intent)>;
            if constexpr (std::is_same_v<T, TrimIntent>) {
                for (const auto id : storage.resident_nodes(intent.segment_endpoint, intent.tier)) {
                    for (std::size_t k = 0; k < 2; ++k) {
                        if (tree_.node(id).hybrid->components[k].on_storage()) { invalidate(id, k); }
                    }
                }
            } else {
                for (const auto id : storage.resident_nodes(intent.source_segment_endpoint, other(intent.destination.tier))) {
                    for (std::size_t k = 0; k < 2; ++k) {
                        const auto& component = tree_.node(id).hybrid->components[k];
                        if (!component.on_storage()) { continue; }
                        quote.queue += component.storage_units * config_.block_size_bytes /
                            c.storage_read_bytes_per_second[storage_tier_index(other(intent.destination.tier))];
                        invalidate(id, k);
                        program({id, k}, intent.destination);
                    }
                }
            }
        }, action);
    }
    for (const auto& write : quote.writes) { program(write.object, write.placement); }
    for (std::size_t k = 0; k < 2; ++k) { quote.media += quote.nand_bytes[k] / 1073741824 * c.nand_seconds_per_gib[k]; }
}

HybridCostQuote JointMemoryCostPolicy::select(
    const std::vector<std::vector<HybridCostObject>>& candidates, HybridCostObject incoming,
    StoragePolicy& policy, const StorageView& storage, TimestampNs now) {
    std::vector<HybridCostQuote> quotes;
    for (const auto& objects : candidates) {
        quotes.push_back(*draft(objects, {}, policy, storage, now));
        for (const auto tier : {StorageTier::Slc, StorageTier::Tlc}) {
            const auto streams = tier == StorageTier::Slc ? config_.slc.stream_count : config_.tlc.stream_count;
            for (std::uint32_t stream = 0; stream < streams; ++stream) {
                const auto quote = draft(objects, Placement{tier, stream}, policy, storage, now);
                if (quote) { quotes.push_back(*quote); } else { ++counters_.infeasible; }
            }
        }
    }
    const auto scenes = scenarios(quotes, now);
    std::vector<HybridCopyChange> admission{{incoming.id, incoming.kind, false, false, {}, true}};
    const auto& pending = *current_request_;
    const auto found = std::find(pending.hash_ids.begin(), pending.hash_ids.end(), incoming.id);
    const auto position = static_cast<std::size_t>(found - pending.hash_ids.begin());
    const auto& hybrid = config_.hybrid_attention;
    // Price the committed request's remaining publications. They are quotation
    // promises only: cache hits still inspect actual published readable copies.
    for (std::size_t i = position; i < pending.hash_ids.size(); ++i) {
        const auto id = pending.hash_ids[i];
        if (i > position) {
            admission.push_back({id, 0, false, false, {}, true, false, true, hybrid.full_block_bytes});
        }
        const bool checkpoint = hybrid.checkpoint_generation == "request_end" ?
            i + 1 == pending.hash_ids.size() : (i + 1) * hybrid.tokens_per_block % hybrid.checkpoint_interval_tokens == 0;
        if (checkpoint && (i > position || incoming.kind == 0)) {
            const auto& node = tree_.node(id);
            const bool readable = node.hybrid && (node.hybrid->components[1].in_dram ||
                node.hybrid->components[1].on_storage());
            if (!readable) {
                admission.push_back({id, 1, false, false, {}, true, false, true, hybrid.linear_checkpoint_bytes});
            }
        }
    }
    const auto base = value(scenes, admission, policy, storage, now);
    std::optional<std::size_t> best;
    for (std::size_t i = 0; i < quotes.size(); ++i) {
        auto& q = quotes[i];
        const auto evaluate = [&](bool writes, bool capacity) {
            auto delta = admission;
            const auto extra = changes(q, writes, capacity);
            delta.insert(delta.end(), extra.begin(), extra.end());
            return value(scenes, delta, policy, storage, now);
        };
        const auto dropped = evaluate(false, false), persisted = evaluate(true, false);
        q.drop = dropped - base;
        q.benefit = dropped - persisted;
        q.replacement = evaluate(true, true) - persisted;
        media_quote(q, now, storage);
        q.total = q.drop - q.benefit + q.replacement +
            config_.hybrid_attention.cost.wear_weight * q.media + q.queue;
        if (!std::isfinite(q.total)) { throw std::overflow_error("joint cost quotation produced a nonfinite cost"); }
        if (!best || q.total / q.released_bytes < quotes[*best].total / quotes[*best].released_bytes) { best = i; }
    }
    if (!best) { throw std::logic_error("joint cost policy has no DRAM eviction candidate"); }
    ++counters_.decisions;
    counters_.quotes += quotes.size();
    auto result = std::move(quotes[*best]);
    counters_.drop += result.drop; counters_.benefit += result.benefit;
    counters_.replacement += result.replacement; counters_.media += result.media;
    counters_.queue += result.queue; counters_.total += result.total;
    for (std::size_t k = 0; k < 2; ++k) { counters_.predicted_nand_bytes[k] += result.nand_bytes[k]; }
    for (const auto& write : result.writes) { ++counters_.selected_routes[storage_tier_index(write.placement.tier)]; }
    return result;
}

} // namespace dwpdsim
