#include "dwpdsim/hybrid_recovery.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace dwpdsim {

HybridRecoveryState::HybridRecoveryState(const RadixTree& tree,
    const std::vector<HybridCopyChange>& changes) : tree_(tree) {
    for (const auto& change : changes) {
        auto& slot = changes_[change.id][change.kind];
        if (!slot) { slot = change; }
        else {
            slot->remove_dram |= change.remove_dram;
            slot->remove_storage |= change.remove_storage;
            if (change.add_storage) { slot->add_storage = change.add_storage; }
        }
    }
}

HybridCopyView HybridRecoveryState::component(NodeId id, std::size_t kind) const {
    if (!tree_.contains(id) || !tree_.node(id).hybrid) { return {}; }
    const auto& node = tree_.node(id);
    const auto& c = node.hybrid->components[kind];
    HybridCopyView result{c.published, c.in_dram,
        c.on_storage() ? std::optional<StorageTier>(node.storage_tier) : std::nullopt,
        c.size_bytes};
    const auto changed = changes_.find(id);
    if (changed != changes_.end() && changed->second[kind]) {
        const auto& change = *changed->second[kind];
        if (change.remove_dram) { result.in_dram = false; }
        if (change.remove_storage) { result.storage.reset(); }
        if (change.add_storage) { result.storage = change.add_storage; }
    }
    return result;
}

HybridRecoveryModel::HybridRecoveryModel(std::uint64_t tokens_per_block,
    std::uint64_t allocation_bytes, std::array<double, 2> storage_bytes_per_second,
    double gpu_bytes_per_second, double prefill_tokens_per_second)
    : tokens_per_block_(tokens_per_block), allocation_bytes_(allocation_bytes),
      storage_bandwidth_(storage_bytes_per_second), gpu_bandwidth_(gpu_bytes_per_second),
      prefill_throughput_(prefill_tokens_per_second) {
    if (!tokens_per_block || !allocation_bytes) {
        throw std::invalid_argument("recovery model block sizes must be positive");
    }
    for (const auto rate : {storage_bandwidth_[0], storage_bandwidth_[1],
            gpu_bandwidth_, prefill_throughput_}) {
        if (!std::isfinite(rate) || rate <= 0) {
            throw std::invalid_argument("recovery model rates must be finite and positive");
        }
    }
}

HybridRecoveryValue HybridRecoveryModel::evaluate(const HybridPrefillRequest& q,
    const HybridRecoveryState& state) const {
    const auto input = q.input_tokens ? q.input_tokens : q.hash_ids.size() * tokens_per_block_;
    const auto limit = std::min(input - 1,
        q.max_prefix_tokens.value_or(std::numeric_limits<std::uint64_t>::max()));
    HybridRecoveryValue result;
    bool continuous = true;
    std::uint64_t full_payload = 0;
    std::array<std::uint64_t, 2> full_reads{};
    const auto add_storage_read = [&](const HybridCopyView& c,
            std::array<std::uint64_t, 2>& reads) {
        if (!c.in_dram) {
            const auto units = c.size_bytes / allocation_bytes_ +
                (c.size_bytes % allocation_bytes_ != 0);
            reads[*c.storage == StorageTier::Slc ? 0 : 1] += units * allocation_bytes_;
        }
    };
    for (std::size_t i = 0; i < q.hash_ids.size(); ++i) {
        const auto id = q.hash_ids[i];
        const auto full = state.component(id, 0);
        if (!full.published) { break; }
        const auto tokens = (i + 1) * tokens_per_block_;
        result.matched_tokens = tokens;
        continuous = continuous && full.available();
        if (!continuous) { continue; }
        result.full_available_tokens = tokens;
        full_payload += full.size_bytes;
        add_storage_read(full, full_reads);
        const auto linear = state.component(id, 1);
        if (tokens <= limit && linear.available()) {
            result.restored_tokens = tokens;
            result.restore_id = id;
            result.storage_read_bytes = full_reads;
            add_storage_read(linear, result.storage_read_bytes);
            result.gpu_transfer_bytes = full_payload + linear.size_bytes;
        }
    }
    result.load_seconds = std::max(
        result.storage_read_bytes[0] / storage_bandwidth_[0],
        result.storage_read_bytes[1] / storage_bandwidth_[1]) +
        result.gpu_transfer_bytes / gpu_bandwidth_;
    result.compute_seconds = (input - result.restored_tokens) / prefill_throughput_;
    result.total_seconds = result.load_seconds + result.compute_seconds;
    return result;
}

} // namespace dwpdsim
