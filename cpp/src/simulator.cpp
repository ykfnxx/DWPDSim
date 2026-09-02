#include "dwpdsim/simulator.hpp"

#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <stdexcept>
#include <utility>

namespace dwpdsim {

Simulator::Simulator(
    SimulationConfig config,
    std::unique_ptr<MemoryPolicy> memory_policy,
    std::unique_ptr<WritePlacementPolicy> placement_policy,
    std::unique_ptr<StorageEvictionPolicy> storage_eviction_policy,
    const std::filesystem::path& trace_path
)
    : config_(validate_config(std::move(config))),
      memory_capacity_blocks_(config_.memory_capacity_bytes / config_.block_size_bytes),
      storage_(config_),
      memory_policy_(std::move(memory_policy)),
      placement_policy_(std::move(placement_policy)),
      storage_eviction_policy_(std::move(storage_eviction_policy)),
      metrics_(config_),
      trace_writer_(trace_path, config_.block_size_bytes) {}

void Simulator::process_request(
    Timestamp timestamp,
    const HashId* hash_ids,
    std::size_t hash_count
) {
    if (last_timestamp_.has_value() && timestamp < *last_timestamp_) {
        throw std::invalid_argument("request timestamp moved backwards");
    }
    last_timestamp_ = timestamp;
    metrics_.record_request(timestamp);

    NodeId parent_id = kRootNodeId;
    const std::uint64_t request_index = metrics_.request_count - 1;
    for (std::size_t position = 0; position < hash_count; ++position) {
        const NodeId node_id = tree_.get_or_create(parent_id, hash_ids[position], timestamp).first;
        const AccessContext context{
            timestamp,
            next_access_sequence_++,
            request_index,
            static_cast<std::uint64_t>(position),
            node_id,
            parent_id,
        };
        process_access(context);
        parent_id = node_id;
    }
}

void Simulator::process_request(Timestamp timestamp, const std::vector<HashId>& hash_ids) {
    process_request(timestamp, hash_ids.data(), hash_ids.size());
}

void Simulator::finish() {
    trace_writer_.finish();
}

const SimulationConfig& Simulator::config() const noexcept {
    return config_;
}

const MetricsCollector& Simulator::metrics() const noexcept {
    return metrics_;
}

const RadixTree& Simulator::tree() const noexcept {
    return tree_;
}

const StorageState& Simulator::storage() const noexcept {
    return storage_;
}

std::uint64_t Simulator::trace_event_count() const noexcept {
    return trace_writer_.event_count();
}

std::uint64_t Simulator::memory_capacity_blocks() const noexcept {
    return memory_capacity_blocks_;
}

SimulationConfig Simulator::validate_config(SimulationConfig config) {
    if (config.block_size_bytes == 0) {
        throw std::invalid_argument("block_size_bytes must be positive");
    }
    if (
        config.memory_capacity_bytes < config.block_size_bytes ||
        config.memory_capacity_bytes % config.block_size_bytes != 0
    ) {
        throw std::invalid_argument("memory capacity must contain whole blocks");
    }
    for (const MediumConfig* medium : {&config.slc, &config.tlc}) {
        if (
            medium->capacity_bytes == 0 ||
            medium->capacity_bytes % config.block_size_bytes != 0
        ) {
            throw std::invalid_argument("storage capacity must contain whole blocks");
        }
        if (medium->stream_count == 0) {
            throw std::invalid_argument("stream_count must be positive");
        }
    }
    return config;
}

void Simulator::process_access(const AccessContext& context) {
    Node& node = tree_.node(context.node_id);
    AccessResult result;

    if (node.in_memory) {
        result = AccessResult::MemoryHit;
        memory_policy_->on_memory_access(context.node_id);
    } else if (node.storage_location.has_value()) {
        const StorageLocation location = *node.storage_location;
        result = location.medium == Medium::Slc ? AccessResult::SlcHit : AccessResult::TlcHit;
        trace_writer_.emit(
            context,
            Operation::Read,
            node,
            location,
            TraceReason::StorageHit
        );
        metrics_.record_io(Operation::Read, location.medium, location.stream_id);
        storage_eviction_policy_->on_storage_read(context.node_id, location.medium);

        if (memory_policy_->admit_storage_hit(context, node)) {
            ++metrics_.storage_promotions;
            insert_into_memory(context.node_id, context);
        } else {
            ++metrics_.storage_bypasses;
        }
    } else {
        result = AccessResult::GlobalMiss;
        insert_into_memory(context.node_id, context);
    }

    tree_.record_access(
        context.node_id,
        context.timestamp,
        result != AccessResult::GlobalMiss
    );
    metrics_.record_access(result);
}

void Simulator::insert_into_memory(NodeId node_id, const AccessContext& context) {
    if (memory_used_blocks_ == memory_capacity_blocks_) {
        evict_from_memory(context);
    }

    Node& node = tree_.node(node_id);
    node.in_memory = true;
    ++memory_used_blocks_;
    memory_policy_->on_memory_insert(node_id);
    metrics_.memory_inserted(node.storage_location.has_value());
}

void Simulator::evict_from_memory(const AccessContext& context) {
    const NodeId victim_id = memory_policy_->choose_victim(context);
    Node& victim = tree_.node(victim_id);
    ++metrics_.memory_evictions;

    if (victim.storage_location.has_value()) {
        ++metrics_.memory_evictions_with_storage_copy;
    } else {
        const EvictionAction action = memory_policy_->eviction_action(victim, context);
        if (action == EvictionAction::Persist) {
            ++metrics_.memory_eviction_persists;
            write_to_storage(victim_id, context);
        } else {
            ++metrics_.memory_eviction_drops;
        }
    }

    metrics_.memory_removed(victim.storage_location.has_value());
    victim.in_memory = false;
    --memory_used_blocks_;
    memory_policy_->on_memory_remove(victim_id);
}

void Simulator::write_to_storage(NodeId node_id, const AccessContext& context) {
    Node& node = tree_.node(node_id);
    const Placement placement = placement_policy_->place(node, context, storage_.summary());
    MediumState& medium = storage_.medium(placement.medium);

    if (medium.full()) {
        const NodeId victim_id = storage_eviction_policy_->choose_victim(
            placement.medium,
            node_id,
            context
        );
        trim_from_storage(victim_id, context);
    }

    const std::uint64_t address = medium.allocate();
    node.storage_location = StorageLocation{placement.medium, address, placement.stream_id};
    storage_eviction_policy_->on_storage_write(node_id, placement.medium);
    metrics_.storage_written(placement.medium, node.in_memory);
    metrics_.record_io(Operation::Write, placement.medium, placement.stream_id);
    trace_writer_.emit(
        context,
        Operation::Write,
        node,
        *node.storage_location,
        TraceReason::MemoryEviction
    );
}

void Simulator::trim_from_storage(NodeId node_id, const AccessContext& context) {
    Node& node = tree_.node(node_id);
    const StorageLocation location = *node.storage_location;
    trace_writer_.emit(
        context,
        Operation::Trim,
        node,
        location,
        TraceReason::StorageEviction
    );
    metrics_.record_io(Operation::Trim, location.medium, location.stream_id);
    storage_eviction_policy_->on_storage_remove(node_id, location.medium);
    storage_.medium(location.medium).release(location.block_address);
    metrics_.storage_removed(location.medium, node.in_memory);
    node.storage_location.reset();
}

}  // namespace dwpdsim
