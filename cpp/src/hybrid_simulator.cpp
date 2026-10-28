#include "dwpdsim/simulator.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <string_view>

namespace dwpdsim {
namespace {

bool drop_next(const HybridAttentionConfig& cfg, const HybridCounters& stats, std::size_t kind) {
    const auto quota = kind == 0 ? cfg.full_drop_fraction : cfg.linear_drop_fraction;
    return quota ? stats.drop_objects < static_cast<std::uint64_t>(
        static_cast<long double>(stats.eligible_evictions + 1) * *quota + cfg.drop_phase) : !cfg.dump_on_eviction;
}

} // namespace

void Simulator::hybrid_require_enabled() const {
    if (!hybrid_) { throw std::logic_error("hybrid_attention.enabled must be true"); }
}

void Simulator::hybrid_validate_order(TimestampNs now, std::uint64_t sequence) const {
    if (finished_) { throw std::logic_error("cannot process events after finish"); }
    if ((last_timestamp_ns_ && now < *last_timestamp_ns_) ||
        (hybrid_->last_sequence && sequence <= *hybrid_->last_sequence)) {
        throw std::invalid_argument("hybrid timestamps must be nondecreasing and sequences strictly increasing");
    }
}

const HybridRuntime& Simulator::hybrid_stats() const {
    hybrid_require_enabled();
    return *hybrid_;
}

const HybridComponent* Simulator::hybrid_component(NodeId id, std::size_t kind) const {
    if (!tree_.contains(id) || !tree_.node(id).hybrid) { return nullptr; }
    return &tree_.node(id).hybrid->components[kind];
}

HybridComponent& Simulator::hybrid_data(NodeId id, std::size_t kind) {
    auto& node = tree_.node(id);
    return node.hybrid.ensure().components[kind];
}

void Simulator::hybrid_create_path(const std::vector<HashId>& path, TimestampNs now) {
    std::optional<NodeId> parent;
    for (const auto id : path) {
        const auto created = parent ? tree_.get_or_create(*parent, id, now) : tree_.get_or_create_root(id, now);
        if (!created.second && tree_.parent(id) != parent) {
            throw std::invalid_argument("prefix hash has a different parent");
        }
        if (created.second) {
            hybrid_->index.on_node_created(id, tree_);
            if (!config_.infinite_storage) { storage_policy_->on_node_created(id, storage_view()); }
        }
        parent = id;
    }
}

std::uint64_t Simulator::hybrid_io(NodeId id, std::size_t kind, const char* operation,
    TimestampNs now, TraceReason reason, std::optional<MoveId> move,
    std::optional<std::uint64_t> dependency, std::optional<StorageLocation> location) {
    const auto sequence = hybrid_->io_sequence++;
    const auto& c = hybrid_data(id, kind);
    if (!config_.infinite_storage) {
        const auto source = location.value_or(tree_.node(id).storage_location());
        auto& counts = hybrid_->storage_counters[storage_tier_index(source.tier)];
        const auto bytes = c.storage_units * config_.block_size_bytes;
        if (std::string_view(operation) == "READ") { ++counts.reads; counts.read_bytes += bytes; }
        else if (std::string_view(operation) == "WRITE") { ++counts.writes; counts.write_bytes += bytes; }
        else { ++counts.trims; counts.trim_bytes += bytes; }
        if (hybrid_->cost) {
            hybrid_->cost->io(id, kind, std::string_view(operation) == "READ" ? Operation::Read :
                std::string_view(operation) == "WRITE" ? Operation::Write : Operation::Trim,
                source, bytes, now, reason, sequence, dependency);
        }
    }
    if (!config_.hybrid_attention.emit_io_trace) { return sequence; }
    auto& out = hybrid_->io;
    out << sequence << ',' << now << ',' << (kind == 0 ? "full" : "linear")
        << ',' << id << ',' << operation << ',' << c.size_bytes << ','
        << c.storage_units * config_.block_size_bytes;
    out << ',';
    for (std::size_t i = 0; i < c.storage_addresses.size(); ++i) {
        if (i) { out << ';'; }
        out << c.storage_addresses[i];
    }
    if (!config_.infinite_storage) {
        const auto source = location.value_or(tree_.node(id).storage_location());
        const char* reason_text = reason == TraceReason::MemoryDump ? "MEMORY_DUMP" :
            reason == TraceReason::CapacityEviction ? "CAPACITY_EVICTION" :
            reason == TraceReason::IdleEviction ? "IDLE_EVICTION" :
            reason == TraceReason::AccessMigration ? "ACCESS_MIGRATION" :
            reason == TraceReason::BackgroundMigration ? "BACKGROUND_MIGRATION" : "STORAGE_HIT";
        out << ',' << (source.tier == StorageTier::Slc ? "slc" : "tlc") << ','
            << source.stream_id << ',' << reason_text << ',';
        if (move) { out << *move; }
        out << ',';
        if (dependency) { out << *dependency; }
    }
    out << '\n';
    return sequence;
}

void Simulator::hybrid_write(NodeId id, std::size_t kind, TimestampNs now,
    std::optional<Placement> quoted_placement) {
    auto& c = hybrid_data(id, kind);
    if (c.on_storage()) { return; }
    const auto units = c.size_bytes / config_.block_size_bytes +
        (c.size_bytes % config_.block_size_bytes != 0);
    StorageLocation placement{StorageTier::Tlc, 0, 0};
    if (!config_.infinite_storage) {
        auto& node = tree_.node(id);
        const auto segment = storage_view().resolve_segment(tree_.segment_leaf_for(id));
        const NodeId incoming[] = {id};
        const RequestContext request{now, hybrid_->current_request_id,
            hybrid_->current_affinity_id, {}, {}};
        const auto decision = quoted_placement ? DumpPlacementDecision{*quoted_placement} :
            storage_policy_->place_dump(DumpContext{
            request, segment, NodeSpan{incoming, 1}, units,
            units * config_.block_size_bytes, NodeSpan{incoming, 1}}, storage_view());
        // Existing policies manage one SSD bundle per prefix node. Components
        // retain separate copies but share that bundle's tier and stream.
        placement = node.on_storage ? node.storage_location() :
            StorageLocation{decision.placement.tier, 0, decision.placement.stream_id};
        if (quoted_placement) {
            if (storage_.tier(placement.tier).used_blocks() + units >
                storage_policy_->capacity_limit_blocks(placement.tier, storage_view())) {
                throw std::logic_error("joint cost quote did not reserve enough storage capacity");
            }
        } else if (!hybrid_ensure_capacity(placement.tier, units, {id}, now)) {
            ++hybrid_->counters[kind].admission_rejections;
            return;
        }
        node.set_storage_location(placement);
    }
    auto& store = storage_.tier(placement.tier);
    if (!store.has_free_blocks(units)) { throw std::overflow_error("hybrid storage address space exhausted"); }
    c.storage_units = units;
    if (config_.hybrid_attention.emit_io_trace || !config_.infinite_storage) {
        c.storage_addresses.reserve(units);
        for (std::uint64_t i = 0; i < units; ++i) { c.storage_addresses.push_back(store.allocate()); }
    } else { store.record_unaddressed_write(units); }
    auto& stats = hybrid_->counters[kind];
    ++stats.write_objects;
    stats.write_bytes += c.size_bytes;
    stats.write_allocated_bytes += units * config_.block_size_bytes;
    stats.storage_bytes += c.size_bytes;
    stats.storage_allocated_bytes += units * config_.block_size_bytes;
    hybrid_io(id, kind, "WRITE", now, TraceReason::MemoryDump);
    if (!config_.infinite_storage) {
        const NodeId incoming[] = {id};
        notify_storage_commit(StorageMutation{StorageMutationKind::DumpWriteCommitted,
            now, hybrid_->current_affinity_id, tree_.segment_leaf_for(id),
            NodeSpan{incoming, 1}, Placement{placement.tier, placement.stream_id},
            placement.tier, units * config_.block_size_bytes});
    }
}

void Simulator::hybrid_cost_evict(NodeId incoming, std::size_t kind,
    std::uint64_t required_bytes, TimestampNs now) {
    const auto& cost_config = config_.hybrid_attention.cost;
    const auto quantum = cost_config.reclaim_bytes ? cost_config.reclaim_bytes :
        config_.hybrid_attention.linear_checkpoint_bytes;
    const auto candidates = hybrid_->index.cost_candidates(tree_, std::max(required_bytes, quantum),
        cost_config.candidates_per_kind);
    const auto quote = hybrid_->cost->select(candidates, {incoming, kind},
        *storage_policy_, storage_view(), now);
    for (const auto& action : quote.capacity_actions) {
        if (!execute_action(action, ActionPlane::Capacity, quote.protected_nodes,
                TraceContext{now, hybrid_->current_request_id, {}})) {
            throw std::logic_error("selected joint cost capacity action failed");
        }
    }
    for (const auto& write : quote.writes) { hybrid_write(write.object.id, write.object.kind, now, write.placement); }
    for (const auto& v : quote.victims) {
        auto& component = hybrid_data(v.id, v.kind);
        auto& stats = hybrid_->counters[v.kind];
        const auto wrote = std::any_of(quote.writes.begin(), quote.writes.end(), [&](const auto& w) {
            return w.object.id == v.id && w.object.kind == v.kind;
        });
        if (wrote) {
            ++stats.eligible_evictions; stats.eligible_bytes += component.size_bytes;
            ++stats.dump_objects; stats.dump_bytes += component.size_bytes;
        } else if (component.on_storage()) {
            ++stats.backed_evictions; stats.backed_bytes += component.size_bytes;
        } else {
            ++stats.eligible_evictions; stats.eligible_bytes += component.size_bytes;
            ++stats.drop_objects; stats.lost_bytes += component.size_bytes;
            ++stats.drop_action_objects; stats.drop_action_bytes += component.size_bytes;
        }
        stats.evicted_bytes += component.size_bytes;
        stats.resident_bytes -= component.size_bytes;
        --stats.resident_objects;
        ++stats.evictions;
        component.in_dram = false;
        hybrid_->index.remove(v.kind, v.id);
        hybrid_->cross_pool_reclaims += v.kind != kind;
    }
}

void Simulator::hybrid_update_pool_targets(std::uint64_t full_blocks, std::uint64_t tokens, TimestampNs now) {
    const auto& cfg = config_.hybrid_attention;
    if (cfg.pool_allocation != "adaptive") { return; }
    auto& state = *hybrid_;
    const long double decay = state.last_pool_update ? std::exp(
        -static_cast<long double>(now - *state.last_pool_update) / (1e9L * cfg.pool_decay_seconds)) : 0;
    state.demand_mass[0] = state.demand_mass[0] * decay +
        static_cast<long double>(full_blocks) * cfg.full_block_bytes;
    const auto first_checkpoint = cfg.checkpoint_generation == "request_end" ?
        cfg.tokens_per_block : cfg.checkpoint_interval_tokens;
    state.demand_mass[1] = state.demand_mass[1] * decay +
        (tokens > first_checkpoint ? cfg.linear_checkpoint_bytes : 0);
    const auto total = config_.memory.capacity_bytes;
    const auto mass = state.demand_mass[0] + state.demand_mass[1];
    const auto full_target = mass > 0 ? static_cast<std::uint64_t>(
        total * state.demand_mass[0] / mass) : total - cfg.linear_checkpoint_bytes;
    state.target_bytes[0] = std::clamp(full_target, cfg.full_block_bytes,
        total - cfg.linear_checkpoint_bytes);
    state.target_bytes[1] = total - state.target_bytes[0];
    state.last_pool_update = now;
    ++state.pool_updates;
}

void Simulator::hybrid_admit(NodeId id, std::size_t kind, TimestampNs now) {
    auto& c = hybrid_data(id, kind);
    const auto& cfg = config_.hybrid_attention;
    const bool shared = cfg.pool_allocation == "shared";
    const bool adaptive = cfg.pool_allocation == "adaptive";
    const auto capacity = shared || adaptive ? config_.memory.capacity_bytes :
        (kind == 0 ? cfg.full_capacity_bytes : cfg.linear_capacity_bytes);
    auto& stats = hybrid_->counters[kind];
    if (c.in_dram) { return; }
    if (c.size_bytes > capacity) { ++stats.bypasses; return; }
    const auto used = [&]() {
        return shared || adaptive ? hybrid_->counters[0].resident_bytes + hybrid_->counters[1].resident_bytes :
            stats.resident_bytes;
    };
    while (c.size_bytes > capacity - used()) {
        if (hybrid_->cost) {
            hybrid_cost_evict(id, kind, c.size_bytes - (capacity - used()), now);
            continue;
        }
        std::size_t pool = kind;
        if (adaptive) {
            // Project the pending admission; unused target bytes can be borrowed.
            std::array<long double, 2> excess;
            for (std::size_t k = 0; k < 2; ++k) {
                excess[k] = static_cast<long double>(hybrid_->counters[k].resident_bytes) +
                    (k == kind ? c.size_bytes : 0) - hybrid_->target_bytes[k];
            }
            pool = excess[1] > excess[0] ? 1 : 0;
            if (!hybrid_->counters[pool].resident_objects) { pool = 1 - pool; }
            if (pool != kind) { ++hybrid_->cross_pool_reclaims; }
        }
        HybridEvictionDecision decision;
        if (shared) {
            decision = hybrid_->index.decide_shared(tree_, cfg, now, hybrid_->counters,
                {drop_next(cfg, hybrid_->counters[0], 0), drop_next(cfg, hybrid_->counters[1], 1)},
                kind, id, capacity);
            pool = decision.kind;
            if (pool != kind) { ++hybrid_->cross_pool_reclaims; }
        } else {
            decision = hybrid_->index.decide(pool, tree_, cfg, now,
                hybrid_->counters[pool].candidates_examined,
                pool == kind ? std::optional<NodeId>(id) : std::nullopt,
                drop_next(cfg, hybrid_->counters[pool], pool),
                adaptive ? hybrid_->target_bytes[pool] :
                    (pool == 0 ? cfg.full_capacity_bytes : cfg.linear_capacity_bytes));
        }
        auto& evicted = hybrid_->counters[pool];
        for (auto victim : decision.victims) {
            auto& v = hybrid_data(victim, pool);
            const bool drop = cfg.eviction_mode == "retention" ? decision.drop : drop_next(cfg, evicted, pool);
            evicted.evicted_bytes += v.size_bytes;
            if (drop) {
                ++evicted.drop_action_objects;
                evicted.drop_action_bytes += v.size_bytes;
            }
            if (v.on_storage()) {
                ++evicted.backed_evictions;
                evicted.backed_bytes += v.size_bytes;
            } else {
                ++evicted.eligible_evictions;
                evicted.eligible_bytes += v.size_bytes;
                if (drop) { ++evicted.drop_objects; evicted.lost_bytes += v.size_bytes; }
                else {
                    hybrid_write(victim, pool, now);
                    if (v.on_storage()) { ++evicted.dump_objects; evicted.dump_bytes += v.size_bytes; }
                    else { ++evicted.drop_objects; evicted.lost_bytes += v.size_bytes; }
                }
            }
            v.in_dram = false;
            evicted.resident_bytes -= v.size_bytes;
            --evicted.resident_objects;
            ++evicted.evictions;
            hybrid_->index.remove(pool, victim);
            // FULL segment selection returns its entire tail in deterministic order.
        }
    }
    c.in_dram = true;
    c.last_touch_ns = now;
    stats.resident_bytes += c.size_bytes;
    ++stats.resident_objects;
    stats.peak_resident_bytes = std::max(stats.peak_resident_bytes, stats.resident_bytes);
    hybrid_->index.touch(kind, id);
    hybrid_->peak_resident_bytes = std::max(hybrid_->peak_resident_bytes,
        hybrid_->counters[0].resident_bytes + hybrid_->counters[1].resident_bytes);
}

void Simulator::hybrid_publish(NodeId id, std::size_t kind, std::uint64_t size, TimestampNs now) {
    auto& c = hybrid_data(id, kind);
    auto& stats = hybrid_->counters[kind];
    ++stats.publications;
    stats.published_bytes += size;
    c.published = true;
    c.size_bytes = size;
    if (hybrid_->cost) { hybrid_->cost->published(id, kind, now); }
    // A duplicate of an already readable immutable object is not a cache touch.
    if (c.in_dram || c.on_storage()) { return; }
    const auto& cfg = config_.hybrid_attention;
    const auto capacity = cfg.pool_allocation != "fixed" ? config_.memory.capacity_bytes :
        (kind == 0 ? cfg.full_capacity_bytes : cfg.linear_capacity_bytes);
    if (size > capacity) {
        ++stats.bypasses;
        hybrid_write(id, kind, now);
    } else { hybrid_admit(id, kind, now); }
}

void Simulator::hybrid_checkpoint_metadata(NodeId id, std::uint64_t tokens,
    std::optional<NodeId> previous, TimestampNs now, bool frontier) {
    auto& c = hybrid_data(id, 1);
    c.checkpoint_tokens = tokens;
    c.previous_checkpoint = previous;
    if (!c.published) {
        c.prefix_frequency = 1;
        c.last_prefix_ns = now;
        c.target_frequency = frontier ? config_.hybrid_attention.tail_prior : 0;
        c.last_target_ns = now;
    }
}

std::string Simulator::hybrid_demand(NodeId id, std::size_t kind, TimestampNs now,
    std::vector<std::pair<std::size_t, NodeId>>& reads) {
    auto& c = hybrid_data(id, kind);
    auto& stats = hybrid_->counters[kind];
    const auto& cfg = config_.hybrid_attention;
    if (kind == 1 && (cfg.linear_policy == "frequency" || cfg.linear_policy == "benefit")) {
        const double age = static_cast<double>(now - c.last_demand_ns) / 1e9;
        c.frequency = c.frequency * std::exp(-age / cfg.decay_seconds) + 1.0;
    }
    c.last_demand_ns = now;
    c.last_touch_ns = now;
    ++c.demands;
    ++stats.demands;
    hybrid_->index.reference(kind, id);
    if (c.in_dram) {
        ++stats.dram_hits;
        hybrid_->index.touch(kind, id);
        return "dram";
    }
    if (c.on_storage()) {
        // Synchronous execution: all selected reads complete before promotions.
        const auto sequence = hybrid_io(id, kind, "READ", now);
        if (!config_.infinite_storage) { hybrid_->read_sequences[kind][id] = sequence; }
        ++stats.read_objects;
        stats.read_bytes += c.size_bytes;
        stats.read_allocated_bytes += c.storage_units * config_.block_size_bytes;
        ++stats.storage_hits;
        reads.emplace_back(kind, id);
        return "storage";
    }
    if (!c.published) { ++stats.never_published_misses; return "never_published"; }
    ++stats.lost_misses;
    return "lost";
}

std::optional<HybridResult> Simulator::process_hybrid_event(const HybridEvent& e) {
    hybrid_require_enabled();
    hybrid_validate_order(e.timestamp_ns, e.sequence);
    if (hybrid_->cost && e.action != "observe") {
        throw std::invalid_argument("joint_cost publication and demand require process_hybrid_prefill");
    }
    if (e.action != "observe" && e.action != "publish" && e.action != "request") {
        throw std::invalid_argument("hybrid action must be observe, publish or request; pending/partial events unsupported");
    }
    if (e.action == "request" && config_.hybrid_attention.replay_mode != "exact_objects") {
        throw std::invalid_argument("prefix_replay uses process_hybrid_prefill for requests");
    }
    if (e.components != "full" && e.components != "linear" && e.components != "both") {
        throw std::invalid_argument("components must be full, linear or both");
    }
    if (e.action == "publish" && e.components == "both") {
        throw std::invalid_argument("publish must specify exactly one component");
    }
    if (e.hash_ids.empty()) { throw std::invalid_argument("hybrid path must be nonempty"); }
    const auto boundary = e.restore_blocks == 0 ? e.hash_ids.size() : e.restore_blocks;
    if (boundary > e.hash_ids.size()) { throw std::invalid_argument("restore_blocks exceeds path length"); }
    if (e.action != "request" && e.restore_blocks != 0) {
        throw std::invalid_argument("restore_blocks applies only to requests");
    }
    if (e.action != "publish" && e.size_bytes != 0) {
        throw std::invalid_argument("size_bytes applies only to publish");
    }
    if (e.action == "request" && request_ids_.count(e.request_id)) {
        throw std::invalid_argument("request_id must be unique");
    }
    const auto kind = e.components == "linear" ? 1U : 0U;
    const auto& cfg = config_.hybrid_attention;
    const auto size = e.size_bytes != 0 ? e.size_bytes :
        (kind == 0 ? cfg.full_block_bytes : cfg.linear_checkpoint_bytes);
    if (e.action == "publish") {
        if (cfg.replay_mode == "prefix_replay" && size !=
            (kind == 0 ? cfg.full_block_bytes : cfg.linear_checkpoint_bytes)) {
            throw std::invalid_argument("prefill object has a conflicting immutable size");
        }
        const auto* existing = hybrid_component(e.hash_ids.back(), kind);
        if (existing && existing->published && existing->size_bytes != size) {
            throw std::invalid_argument("immutable object size changed");
        }
    }

    run_until(e.timestamp_ns);
    hybrid_->current_request_id = e.request_id;
    hybrid_->current_affinity_id = 0;
    for (auto& sequences : hybrid_->read_sequences) { sequences.clear(); }
    // Path construction checks parent identity directly; invalid input aborts replay.
    hybrid_create_path(e.hash_ids, e.timestamp_ns);
    last_timestamp_ns_ = e.timestamp_ns;
    hybrid_->last_sequence = e.sequence;
    if (e.action == "observe") { return std::nullopt; }
    if (e.action == "publish") {
        if (kind == 1 && cfg.replay_mode == "prefix_replay") {
            std::optional<NodeId> previous;
            for (std::size_t i = 0; i + 1 < e.hash_ids.size(); ++i) {
                const auto* c = hybrid_component(e.hash_ids[i], 1);
                if (c && c->published) { previous = e.hash_ids[i]; }
            }
            hybrid_checkpoint_metadata(e.hash_ids.back(), e.hash_ids.size() * cfg.tokens_per_block,
                previous, e.timestamp_ns);
        }
        hybrid_publish(e.hash_ids.back(), kind, size, e.timestamp_ns);
        return std::nullopt;
    }

    request_ids_.insert(e.request_id);
    hybrid_->index.begin_request();
    HybridResult result;
    result.sequence = e.sequence;
    result.request_id = e.request_id;
    result.restore_blocks = boundary;
    result.all_available = result.all_dram = e.components == "both";
    std::vector<std::pair<std::size_t, NodeId>> reads;
    auto demand = [&](std::size_t component, NodeId id) -> std::string {
        auto source = hybrid_demand(id, component, e.timestamp_ns, reads);
        if (source != "dram") { result.all_dram = false; }
        if (source != "dram" && source != "storage") { result.all_available = false; }
        return source;
    };
    // Resolve the complete request against one pre-promotion state. No miss can
    // produce data and no promotion can turn a later missing component into a hit.
    if (e.components != "linear") {
        for (std::size_t i = 0; i < boundary; ++i) { result.full.push_back(demand(0, e.hash_ids[i])); }
    }
    if (e.components != "full") { result.linear = demand(1, e.hash_ids[boundary - 1]); }
    ++hybrid_->requests;
    hybrid_->all_available_requests += result.all_available;
    hybrid_->all_dram_requests += result.all_dram;
    if (e.components == "both") {
        ++hybrid_->joint_requests;
        hybrid_->joint_available += result.all_available;
        hybrid_->joint_dram += result.all_dram;
    }
    hybrid_storage_accesses(reads, e.timestamp_ns);
    if (cfg.admit_storage_hits) {
        for (const auto& read : reads) { hybrid_admit(read.second, read.first, e.timestamp_ns); }
    }
    return result;
}

HybridPrefillResult Simulator::process_hybrid_prefill(const HybridPrefillRequest& q, bool collect_results) {
    hybrid_require_enabled();
    const auto& cfg = config_.hybrid_attention;
    if (cfg.replay_mode != "prefix_replay") {
        throw std::invalid_argument("process_hybrid_prefill requires replay_mode=prefix_replay");
    }
    hybrid_validate_order(q.timestamp_ns, q.sequence);
    if (request_ids_.count(q.request_id)) { throw std::invalid_argument("request_id must be unique"); }
    const auto block_tokens = cfg.tokens_per_block;
    if (q.hash_ids.size() > std::numeric_limits<std::uint64_t>::max() / block_tokens) {
        throw std::invalid_argument("prefill token count overflows uint64");
    }
    const auto input_tokens = q.input_tokens ? q.input_tokens : q.hash_ids.size() * block_tokens;
    if (!input_tokens || input_tokens / block_tokens != q.hash_ids.size()) {
        throw std::invalid_argument("input_tokens must name exactly the supplied complete hash blocks");
    }
    run_until(q.timestamp_ns);
    hybrid_->current_request_id = q.request_id;
    hybrid_->current_affinity_id = q.affinity_id;
    for (auto& sequences : hybrid_->read_sequences) { sequences.clear(); }
    hybrid_create_path(q.hash_ids, q.timestamp_ns);
    if (hybrid_->cost) { hybrid_->cost->begin_request(q); }
    if (!config_.infinite_storage) {
        storage_policy_->on_request_begin(RequestContext{q.timestamp_ns, q.request_id,
            q.affinity_id, HashSpan{q.hash_ids.data(), q.hash_ids.size()}, {}}, storage_view());
    }
    hybrid_update_pool_targets(q.hash_ids.size(), input_tokens, q.timestamp_ns);
    HybridPrefillResult result;
    result.sequence = q.sequence;
    result.request_id = q.request_id;
    result.input_tokens = input_tokens;
    const auto limit = std::min(input_tokens - 1,
        q.max_prefix_tokens.value_or(std::numeric_limits<std::uint64_t>::max()));
    bool continuous_full = true;
    std::optional<NodeId> target_checkpoint;
    std::optional<NodeId> previous_before_end;
    const bool score_prefix = cfg.linear_policy == "prefix_frequency" ||
        cfg.linear_policy == "replay_value" || cfg.linear_policy == "copy_aware" ||
        cfg.linear_policy == "target_frequency" || cfg.linear_policy == "target_value";
    for (std::size_t i = 0; i < q.hash_ids.size(); ++i) {
        const auto* full = hybrid_component(q.hash_ids[i], 0);
        if (!full || !full->published) { break; }
        const auto t = (i + 1) * block_tokens;
        result.matched_prefix_tokens = t;
        const auto* linear = hybrid_component(q.hash_ids[i], 1);
        if (linear && linear->published && i + 1 < q.hash_ids.size()) {
            previous_before_end = q.hash_ids[i];
        }
        if (score_prefix && t <= limit && linear && linear->published) {
            target_checkpoint = q.hash_ids[i];
            auto& estimate = hybrid_data(q.hash_ids[i], 1);
            const double age = static_cast<double>(q.timestamp_ns - estimate.last_prefix_ns) / 1e9;
            estimate.prefix_frequency = estimate.prefix_frequency * std::exp(-age / cfg.decay_seconds) + 1;
            estimate.last_prefix_ns = q.timestamp_ns;
        }
        continuous_full = continuous_full && (full->in_dram || full->on_storage());
        if (!continuous_full) { continue; }
        result.full_available_prefix_tokens = t;
        if (t <= limit && linear && (linear->in_dram || linear->on_storage())) {
            result.restore_tokens = t;
            result.restore_hash_id = q.hash_ids[i];
        }
    }

    if (target_checkpoint) {
        // The desired endpoint can be absent. This observation never makes it readable.
        auto& estimate = hybrid_data(*target_checkpoint, 1);
        const double age = static_cast<double>(q.timestamp_ns - estimate.last_target_ns) / 1e9;
        estimate.target_frequency = estimate.target_frequency * std::exp(-age / cfg.decay_seconds) + 1;
        estimate.last_target_ns = q.timestamp_ns;
    }

    last_timestamp_ns_ = q.timestamp_ns;
    hybrid_->last_sequence = q.sequence;
    request_ids_.insert(q.request_id);
    hybrid_->index.begin_request();
    const auto restored_blocks = result.restore_tokens / block_tokens;
    std::vector<std::pair<std::size_t, NodeId>> reads;
    for (std::size_t i = 0; i < restored_blocks; ++i) {
        auto source = hybrid_demand(q.hash_ids[i], 0, q.timestamp_ns, reads);
        if (collect_results) { result.full.push_back(std::move(source)); }
    }
    if (result.restore_hash_id) {
        result.linear = hybrid_demand(*result.restore_hash_id, 1, q.timestamp_ns, reads);
    }
    hybrid_storage_accesses(reads, q.timestamp_ns);
    // The request has consumed every source before a promotion can evict it.
    if (cfg.admit_storage_hits) {
        for (const auto& read : reads) { hybrid_admit(read.second, read.first, q.timestamp_ns); }
    }

    std::uint64_t generated_checkpoints = 0;
    std::optional<NodeId> previous = result.restore_hash_id;
    for (std::size_t i = restored_blocks; i < q.hash_ids.size(); ++i) {
        hybrid_publish(q.hash_ids[i], 0, cfg.full_block_bytes, q.timestamp_ns);
        const auto t = (i + 1) * block_tokens;
        // One complete checkpoint per fixed chunk; no state for the short tail.
        if (cfg.checkpoint_generation == "chunked_prefill" && t % cfg.checkpoint_interval_tokens == 0) {
            const bool frontier = t / cfg.checkpoint_interval_tokens == input_tokens / cfg.checkpoint_interval_tokens;
            hybrid_checkpoint_metadata(q.hash_ids[i], t, previous, q.timestamp_ns, frontier);
            hybrid_publish(q.hash_ids[i], 1, cfg.linear_checkpoint_bytes, q.timestamp_ns);
            ++generated_checkpoints;
            if (collect_results) { result.generated_checkpoint_tokens.push_back(t); }
        }
        const auto* c = hybrid_component(q.hash_ids[i], 1);
        if (c && c->published) {
            previous = q.hash_ids[i];
            if (i + 1 < q.hash_ids.size()) { previous_before_end = q.hash_ids[i]; }
        }
    }
    if (cfg.checkpoint_generation == "request_end" && !q.hash_ids.empty()) {
        // Only complete-block identities are supplied; the short token tail has no hash.
        // Publish once even when this immutable endpoint was the actual restore source.
        const auto t = q.hash_ids.size() * block_tokens;
        hybrid_checkpoint_metadata(q.hash_ids.back(), t, previous_before_end, q.timestamp_ns, true);
        hybrid_publish(q.hash_ids.back(), 1, cfg.linear_checkpoint_bytes, q.timestamp_ns);
        ++generated_checkpoints;
        if (collect_results) { result.generated_checkpoint_tokens.push_back(t); }
    }
    result.prefill_compute_tokens = input_tokens - result.restore_tokens;
    result.replayed_prefix_tokens = result.matched_prefix_tokens - result.restore_tokens;
    result.new_suffix_tokens = input_tokens - result.matched_prefix_tokens;
    ++hybrid_->requests;
    auto& stats = hybrid_->prefill;
    if (result.restore_tokens) {
        ++stats.restored_requests;
        if (reads.empty()) { ++stats.dram_restored_requests; }
    }
    stats.input_tokens += input_tokens;
    stats.matched_prefix_tokens += result.matched_prefix_tokens;
    stats.restored_tokens += result.restore_tokens;
    stats.prefill_compute_tokens += result.prefill_compute_tokens;
    stats.replayed_prefix_tokens += result.replayed_prefix_tokens;
    stats.new_suffix_tokens += result.new_suffix_tokens;
    stats.generated_checkpoints += generated_checkpoints;
    if (hybrid_->cost) { hybrid_->cost->end_request(q.timestamp_ns); }
    return result;
}

} // namespace dwpdsim
