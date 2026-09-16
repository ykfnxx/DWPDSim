#include "dwpdsim/shadow_ftl.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <queue>
#include <stdexcept>
#include <tuple>

namespace dwpdsim {

class ShadowFtl::Pool {
    struct Key {
        std::uint64_t page;
        std::uint32_t stream;
        bool operator==(const Key& other) const { return page == other.page && stream == other.stream; }
    };
    struct Hash {
        std::size_t operator()(const Key& key) const {
            return std::hash<std::uint64_t>{}(key.page) ^ (std::hash<std::uint32_t>{}(key.stream) << 1);
        }
    };
    struct Location { std::size_t block, slot; };
    struct Slot { Key key; bool valid; };
    struct Block {
        std::vector<Slot> slots;
        std::size_t live = 0;
        std::uint32_t stream = 0;
        std::uint64_t erases = 0;
        TimestampNs last_write = 0;
        bool active = false;
    };
    using FreeEntry = std::pair<std::uint64_t, std::size_t>;

  public:
    Pool(std::uint64_t nominal, std::uint32_t streams, const ShadowConfig& config, double endurance, std::uint64_t physical_blocks)
        : nominal_(nominal), page_bytes_(config.page_bytes), pages_per_block_(config.pages_per_block),
          endurance_(endurance), open_(streams, none) {
        const long double blocks = physical_blocks ? physical_blocks : std::ceil(static_cast<long double>(nominal) /
            config.page_bytes / config.pages_per_block / (1 - config.overprovisioning));
        if (blocks > static_cast<long double>(std::numeric_limits<std::size_t>::max())) {
            throw std::invalid_argument("shadow geometry exceeds addressable block count");
        }
        if (physical_blocks && (physical_blocks < streams + 2 ||
            static_cast<long double>(physical_blocks) * config.pages_per_block * config.page_bytes <= nominal)) {
            throw std::invalid_argument("shadow physical geometry needs capacity above nominal and GC/frontier blocks");
        }
        // Small test pools still need one possible frontier per stream plus a GC reserve.
        blocks_.resize(std::max<std::size_t>(streams + 2, static_cast<std::size_t>(blocks)));
        active_limit_ = std::min(blocks_.size() - 1, std::max<std::size_t>(1,
            static_cast<std::size_t>(std::ceil(blocks_.size() * (1 - config.overprovisioning)))));
        for (std::size_t i = 0; i < blocks_.size(); ++i) { free_.push({0, i}); }
    }

    void write(std::uint32_t stream, std::uint64_t page, TimestampNs now) {
        const Key key{page, stream};
        trim(stream, page);
        const auto location = append(key, now, false);
        mapping_[key] = location;
    }
    void trim(std::uint32_t stream, std::uint64_t page) {
        const auto it = mapping_.find(Key{page, stream});
        if (it == mapping_.end()) { return; }
        auto& block = blocks_[it->second.block];
        block.slots[it->second.slot].valid = false;
        --block.live;
        mapping_.erase(it);
    }
    ShadowPoolStats stats(double wa) const {
        return {host_, gc_, erases_, blocks_.size(), nominal_, wa,
            static_cast<double>(host_ + gc_) * page_bytes_ / (nominal_ * endurance_)};
    }

  private:
    static constexpr std::size_t none = std::numeric_limits<std::size_t>::max();
    std::size_t allocate(std::uint32_t stream, TimestampNs now, bool gc) {
        if (!gc) {
            while (active_ >= active_limit_ && collect(now)) {
                if (open_[stream] != none) { return open_[stream]; }
            }
        }
        if (free_.empty() && !gc) { collect(now); }
        if (free_.empty()) { throw std::runtime_error("shadow FTL exhausted: increase nominal capacity or overprovisioning"); }
        const auto id = free_.top().second;
        free_.pop();
        auto& block = blocks_[id];
        block.slots.clear();
        block.live = 0;
        block.stream = stream;
        block.active = true;
        block.last_write = now;
        ++active_;
        open_[stream] = id;
        return id;
    }
    Location append(Key key, TimestampNs now, bool gc) {
        auto id = open_[key.stream];
        if (id == none) { id = allocate(key.stream, now, gc); }
        auto& block = blocks_[id];
        const auto slot = block.slots.size();
        block.slots.push_back({key, true});
        ++block.live;
        block.last_write = now;
        if (gc) { ++gc_; } else { ++host_; }
        if (block.slots.size() == pages_per_block_) { open_[key.stream] = none; }
        return {id, slot};
    }
    bool collect(TimestampNs now) {
        std::size_t victim = none;
        for (std::size_t i = 0; i < blocks_.size(); ++i) {
            const auto& block = blocks_[i];
            if (!block.active || block.slots.size() != pages_per_block_ || block.live == pages_per_block_) { continue; }
            // Most invalid pages, oldest last write, fewest erases, stable block ID.
            if (victim == none ||
                std::tie(block.live, block.last_write, block.erases, i) <
                std::tie(blocks_[victim].live, blocks_[victim].last_write, blocks_[victim].erases, victim)) {
                victim = i;
            }
        }
        if (victim == none) { return false; }
        auto& block = blocks_[victim];
        const auto frontier = open_[block.stream];
        if (free_.empty() && block.live &&
            (frontier == none || pages_per_block_ - blocks_[frontier].slots.size() < block.live)) {
            return false;
        }
        for (const auto& slot : block.slots) {
            if (slot.valid) { mapping_.at(slot.key) = append(slot.key, now, true); }
        }
        block.slots.clear();
        block.live = 0;
        block.active = false;
        ++block.erases;
        ++erases_;
        --active_;
        free_.push({block.erases, victim});
        return true;
    }
    std::uint64_t nominal_, page_bytes_, pages_per_block_;
    double endurance_;
    std::vector<Block> blocks_;
    std::vector<std::size_t> open_;
    std::priority_queue<FreeEntry, std::vector<FreeEntry>, std::greater<FreeEntry>> free_;
    std::unordered_map<Key, Location, Hash> mapping_;
    std::size_t active_ = 0, active_limit_ = 0;
    std::uint64_t host_ = 0, gc_ = 0, erases_ = 0;
};

ShadowFtl::ShadowFtl(const SimulationConfig& simulation, const ShadowConfig& config) : config_(config) {
    if (!config.page_bytes || !config.pages_per_block ||
        simulation.block_size_bytes % config.page_bytes || !config.feedback_period_ns ||
        !std::isfinite(config.overprovisioning) || config.overprovisioning <= 0 || config.overprovisioning >= 1 ||
        !std::isfinite(config.slc_endurance) || config.slc_endurance <= 0 ||
        !std::isfinite(config.tlc_endurance) || config.tlc_endurance <= 0 ||
        !std::isfinite(config.reuse_loss_budget) || config.reuse_loss_budget < 0 || config.reuse_loss_budget >= 1 ||
        !config.min_reuse_blocks || !config.reuse_ema_scale_blocks) {
        throw std::invalid_argument("invalid shadow geometry, feedback period, endurance or reuse configuration");
    }
    pages_per_kv_block_ = simulation.block_size_bytes / config.page_bytes;
    const std::array<StorageTierConfig, 2> tiers{simulation.slc, simulation.tlc};
    const std::array<std::uint64_t, 2> nominal{config.slc_nominal_bytes, config.tlc_nominal_bytes};
    const std::array<double, 2> endurance{config.slc_endurance, config.tlc_endurance};
    const std::array<std::uint64_t, 2> physical{config.slc_physical_blocks, config.tlc_physical_blocks};
    for (std::size_t i = 0; i < 2; ++i) {
        const auto bytes = nominal[i] ? nominal[i] : tiers[i].capacity_bytes;
        if (bytes < tiers[i].capacity_bytes || bytes % config.page_bytes) {
            throw std::invalid_argument("shadow nominal capacity must be page aligned and cover storage capacity");
        }
        pools_[i] = std::make_unique<Pool>(bytes, tiers[i].stream_count, config, endurance[i], physical[i]);
    }
}
ShadowFtl::~ShadowFtl() = default;

void ShadowFtl::consume(Operation op, const StorageLocation& location, TimestampNs now) {
    if (op == Operation::Read) { return; }
    auto& pool = *pools_[storage_tier_index(location.tier)];
    const auto first = location.block_address * pages_per_kv_block_;
    for (std::uint64_t i = 0; i < pages_per_kv_block_; ++i) {
        if (op == Operation::Write) { pool.write(location.stream_id, first + i, now); }
        else { pool.trim(location.stream_id, first + i); }
    }
}

void ShadowFtl::expire_ghosts(TimestampNs now) {
    constexpr TimestampNs ttl = 24ULL * 3600 * 1000000000ULL;
    while (!expirations_.empty() && now > expirations_.begin()->first &&
           now - expirations_.begin()->first > ttl) {
        ghosts_.erase(expirations_.begin()->second);
        expirations_.erase(expirations_.begin());
    }
}
void ShadowFtl::record_loss(NodeId id, TimestampNs now) {
    if (!config_.online_tuning) { return; }
    expire_ghosts(now);
    const auto previous = ghosts_.find(id);
    if (previous != ghosts_.end()) { expirations_.erase({previous->second, id}); }
    ghosts_[id] = now;
    expirations_.insert({now, id});
}
std::uint64_t ShadowFtl::ghost_prefix(const HashId* hashes, std::size_t size, TimestampNs now) {
    expire_ghosts(now);
    std::uint64_t longest = 0;
    // DWPDSim hashes globally identify prefix blocks. An evicted hash at position i
    // is evidence that this prefix existed; no duplicate Radix/path snapshots needed.
    for (std::size_t i = 0; i < size; ++i) {
        if (ghosts_.count(hashes[i])) { longest = i + 1; }
    }
    return longest;
}
void ShadowFtl::record_request(std::uint64_t hits, std::uint64_t misses) {
    hits_ += hits;
    ghost_misses_ += misses;
    total_ghost_misses_ += misses;
}
ShadowFeedback ShadowFtl::stats() const {
    return {0, {pools_[0]->stats(wa_[0]), pools_[1]->stats(wa_[1])}, hits_, ghost_misses_};
}
ShadowFeedback ShadowFtl::close_window(TimestampNs now) {
    expire_ghosts(now);
    auto result = stats();
    result.timestamp_ns = now;
    for (std::size_t i = 0; i < 2; ++i) {
        auto& pool = result.pools[i];
        const auto host = pool.host_program_pages;
        const auto nand = host + pool.gc_program_pages;
        if (host > last_host_[i]) {
            const double wa = std::max(1.0, static_cast<double>(nand - last_nand_[i]) / (host - last_host_[i]));
            wa_[i] = 0.75 * wa_[i] + 0.25 * wa;
        }
        pool.wa_ema = wa_[i];
        last_host_[i] = host;
        last_nand_[i] = nand;
    }
    hits_ = ghost_misses_ = 0;
    return result;
}

}  // namespace dwpdsim
