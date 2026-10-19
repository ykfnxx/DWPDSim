#include "dwpdsim/simulator.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <unordered_set>

namespace dwpdsim {

void Simulator::hybrid_require_enabled() const {
    if (!hybrid_) { throw std::logic_error("hybrid_attention.enabled must be true"); }
}

const HybridRuntime& Simulator::hybrid_stats() const {
    hybrid_require_enabled();
    return *hybrid_;
}

const HybridComponent* Simulator::hybrid_component(NodeId id, std::size_t kind) const {
    hybrid_require_enabled();
    if (kind > 1) { throw std::invalid_argument("invalid attention component"); }
    if (!tree_.contains(id) || !tree_.node(id).hybrid) { return nullptr; }
    return &tree_.node(id).hybrid->components[kind];
}

HybridComponent& Simulator::hybrid_data(NodeId id, std::size_t kind) {
    auto& node = tree_.node(id);
    return node.hybrid.ensure().components[kind];
}

void Simulator::hybrid_validate_path(const std::vector<HashId>& path) const {
    if (path.empty()) { throw std::invalid_argument("hybrid path must be nonempty"); }
    std::unordered_set<HashId> seen;
    std::optional<NodeId> parent;
    for (const auto id : path) {
        if (!seen.insert(id).second) {
            throw std::invalid_argument("a prefix hash cannot repeat within a path");
        }
        if (tree_.contains(id) && tree_.parent(id) != parent) {
            throw std::invalid_argument("prefix hash has a different parent");
        }
        parent = id;
    }
}

void Simulator::hybrid_create_path(const std::vector<HashId>& path, TimestampNs now) {
    std::optional<NodeId> parent;
    for (const auto id : path) {
        if (parent) { tree_.get_or_create(*parent, id, now); }
        else { tree_.get_or_create_root(id, now); }
        parent = id;
    }
}

void Simulator::hybrid_io(NodeId id, std::size_t kind, const char* operation, TimestampNs now) {
    const auto& c = hybrid_data(id, kind);
    auto& out = hybrid_->io;
    out << hybrid_->io_sequence++ << ',' << now << ',' << (kind == 0 ? "full" : "linear")
        << ',' << id << ',' << operation << ',' << c.size_bytes << ','
        << c.storage_addresses.size() * config_.block_size_bytes;
    out << ',';
    for (std::size_t i = 0; i < c.storage_addresses.size(); ++i) {
        if (i) { out << ';'; }
        out << c.storage_addresses[i];
    }
    out << '\n';
}

void Simulator::hybrid_write(NodeId id, std::size_t kind, TimestampNs now) {
    auto& c = hybrid_data(id, kind);
    if (!c.storage_addresses.empty()) { return; }
    const auto units = c.size_bytes / config_.block_size_bytes +
        (c.size_bytes % config_.block_size_bytes != 0);
    auto& store = storage_.tier(StorageTier::Tlc);
    if (!store.has_free_blocks(units)) { throw std::overflow_error("hybrid storage address space exhausted"); }
    c.storage_addresses.reserve(units);
    for (std::uint64_t i = 0; i < units; ++i) { c.storage_addresses.push_back(store.allocate()); }
    auto& stats = hybrid_->counters[kind];
    ++stats.write_objects;
    stats.write_bytes += c.size_bytes;
    stats.write_allocated_bytes += units * config_.block_size_bytes;
    stats.storage_bytes += c.size_bytes;
    stats.storage_allocated_bytes += units * config_.block_size_bytes;
    hybrid_io(id, kind, "WRITE", now);
}

void Simulator::hybrid_admit(NodeId id, std::size_t kind, TimestampNs now) {
    auto& c = hybrid_data(id, kind);
    const auto& cfg = config_.hybrid_attention;
    const auto capacity = kind == 0 ? cfg.full_capacity_bytes : cfg.linear_capacity_bytes;
    auto& stats = hybrid_->counters[kind];
    if (c.in_dram) { return; }
    if (c.size_bytes > capacity) { ++stats.bypasses; return; }
    while (c.size_bytes > capacity - stats.resident_bytes) {
        const auto victims = hybrid_->index.victim(kind, tree_, cfg, now, stats.candidates_examined);
        for (auto victim : victims) {
            auto& v = hybrid_data(victim, kind);
            if (cfg.dump_on_eviction) { hybrid_write(victim, kind, now); }
            v.in_dram = false;
            stats.resident_bytes -= v.size_bytes;
            --stats.resident_objects;
            ++stats.evictions;
            hybrid_->index.remove(kind, victim);
            // FULL segment selection returns its entire tail in deterministic order.
        }
    }
    c.in_dram = true;
    stats.resident_bytes += c.size_bytes;
    ++stats.resident_objects;
    stats.peak_resident_bytes = std::max(stats.peak_resident_bytes, stats.resident_bytes);
    hybrid_->index.touch(kind, id);
}

std::optional<HybridResult> Simulator::process_hybrid_event(const HybridEvent& e) {
    hybrid_require_enabled();
    if (finished_) { throw std::logic_error("cannot process events after finish"); }
    if ((last_timestamp_ns_ && e.timestamp_ns < *last_timestamp_ns_) ||
        (hybrid_->last_sequence && e.sequence <= *hybrid_->last_sequence)) {
        throw std::invalid_argument("hybrid timestamps must be nondecreasing and sequences strictly increasing");
    }
    if (e.action != "observe" && e.action != "publish" && e.action != "request") {
        throw std::invalid_argument("hybrid action must be observe, publish or request; pending/partial events unsupported");
    }
    if (e.components != "full" && e.components != "linear" && e.components != "both") {
        throw std::invalid_argument("components must be full, linear or both");
    }
    if (e.action == "publish" && e.components == "both") {
        throw std::invalid_argument("publish must specify exactly one component");
    }
    hybrid_validate_path(e.hash_ids);
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
        const auto* existing = hybrid_component(e.hash_ids.back(), kind);
        if (existing && existing->published && existing->size_bytes != size) {
            throw std::invalid_argument("immutable object size changed");
        }
    }

    // Validate external boundaries before any mutation or time advancement.
    hybrid_create_path(e.hash_ids, e.timestamp_ns);
    last_timestamp_ns_ = e.timestamp_ns;
    hybrid_->last_sequence = e.sequence;
    if (e.action == "observe") { return std::nullopt; }
    if (e.action == "publish") {
        const auto id = e.hash_ids.back();
        auto& c = hybrid_data(id, kind);
        auto& stats = hybrid_->counters[kind];
        ++stats.publications;
        stats.published_bytes += size;
        c.published = true;
        c.size_bytes = size;
        // A duplicate of an already readable immutable object is not a cache touch.
        if (c.in_dram || !c.storage_addresses.empty()) { return std::nullopt; }
        const auto capacity = kind == 0 ? cfg.full_capacity_bytes : cfg.linear_capacity_bytes;
        if (size > capacity) {
            ++stats.bypasses;
            hybrid_write(id, kind, e.timestamp_ns);
        } else { hybrid_admit(id, kind, e.timestamp_ns); }
        return std::nullopt;
    }

    request_ids_.insert(e.request_id);
    HybridResult result;
    result.sequence = e.sequence;
    result.request_id = e.request_id;
    result.restore_blocks = boundary;
    result.all_available = result.all_dram = true;
    std::vector<std::pair<std::size_t, NodeId>> reads;
    auto demand = [&](std::size_t component, NodeId id) -> std::string {
        auto& c = hybrid_data(id, component);
        auto& stats = hybrid_->counters[component];
        const double age = static_cast<double>(e.timestamp_ns - c.last_demand_ns) / 1e9;
        c.frequency = c.frequency * std::exp(-age / cfg.decay_seconds) + 1.0;
        c.last_demand_ns = e.timestamp_ns;
        ++c.demands;
        ++stats.demands;
        if (c.in_dram) {
            ++stats.dram_hits;
            hybrid_->index.touch(component, id);
            return "dram";
        }
        result.all_dram = false;
        if (!c.storage_addresses.empty()) {
            // Synchronous execution: the READ completes before service is counted.
            hybrid_io(id, component, "READ", e.timestamp_ns);
            ++stats.read_objects;
            stats.read_bytes += c.size_bytes;
            stats.read_allocated_bytes += c.storage_addresses.size() * config_.block_size_bytes;
            ++stats.storage_hits;
            reads.emplace_back(component, id);
            return "storage";
        }
        result.all_available = false;
        if (!c.published) { ++stats.never_published_misses; return "never_published"; }
        ++stats.lost_misses;
        return "lost";
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
    if (cfg.admit_storage_hits) {
        for (const auto& read : reads) { hybrid_admit(read.second, read.first, e.timestamp_ns); }
    }
    return result;
}

} // namespace dwpdsim
