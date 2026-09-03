#include "dwpdsim/simulator.hpp"

#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <stdexcept>
#include <utility>

namespace dwpdsim {

Simulator::Simulator(
    SimulationConfig config,
    std::unique_ptr<MemoryPolicyBase> memory_policy,
    std::unique_ptr<WritePlacementPolicyBase> placement_policy,
    std::unique_ptr<StorageEvictionPolicyBase> storage_eviction_policy,
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

    std::optional<NodeId> parent_id;
    const std::uint64_t request_index = metrics_.request_count - 1;
    for (std::size_t position = 0; position < hash_count; ++position) {
        const auto [node_id, created] = parent_id.has_value()
                                            ? tree_.get_or_create(
                                                  *parent_id,
                                                  hash_ids[position],
                                                  timestamp
                                              )
                                            : tree_.get_or_create_root(
                                                  hash_ids[position],
                                                  timestamp
                                              );
        if (created) {
            ++metrics_.tree_nodes_created;
            notify_node_created(node_id, parent_id);
        }
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
    for (const StorageTierConfig* tier : {&config.slc, &config.tlc}) {
        if (
            tier->capacity_bytes == 0 ||
            tier->capacity_bytes % config.block_size_bytes != 0
        ) {
            throw std::invalid_argument("storage capacity must contain whole blocks");
        }
        if (tier->stream_count == 0) {
            throw std::invalid_argument("stream_count must be positive");
        }
    }
    return config;
}

void Simulator::process_access(const AccessContext& context) {
    active_node_id_ = context.node_id;
    Node& node = tree_.node(context.node_id);
    AccessResult result;

    if (node.in_memory) {
        result = AccessResult::MemoryHit;
        memory_policy_->on_memory_access(context.node_id);
    } else if (node.on_storage) {
        const StorageLocation location = node.storage_location();
        result = location.tier == StorageTier::Slc ? AccessResult::SlcHit : AccessResult::TlcHit;
        trace_writer_.emit(
            context,
            context.node_id,
            Operation::Read,
            node,
            location,
            TraceReason::StorageHit
        );
        metrics_.record_io(Operation::Read, location.tier, location.stream_id);
        storage_eviction_policy_->on_storage_read(context.node_id, location.tier);

        if (memory_policy_->admit_storage_hit(context, node, tree_)) {
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
    notify_access_complete(context, result);
    metrics_.record_access(result);
    active_node_id_.reset();
}

void Simulator::insert_into_memory(NodeId node_id, const AccessContext& context) {
    if (memory_used_blocks_ == memory_capacity_blocks_) {
        evict_from_memory(context);
    }

    Node& node = tree_.node(node_id);
    node.in_memory = true;
    ++memory_used_blocks_;
    memory_policy_->on_memory_insert(node_id);
    metrics_.memory_inserted(node.on_storage);
}

void Simulator::evict_from_memory(const AccessContext& context) {
    const NodeId endpoint = memory_policy_->choose_victim(context, tree_);
    tree_.resolve_segment(endpoint, memory_segment_scratch_);
    ++metrics_.memory_evicted_segments;
    deferred_prune_scratch_.clear();
    defer_storage_prune_ = true;

    for (NodeId victim_id : memory_segment_scratch_) {
        Node& victim = tree_.node(victim_id);
        if (!victim.in_memory) {
            continue;
        }

        ++metrics_.memory_evictions;
        if (victim.on_storage) {
            ++metrics_.memory_evictions_with_storage_copy;
        } else {
            const EvictionAction action = memory_policy_->eviction_action(
                victim,
                context,
                tree_
            );
            if (action == EvictionAction::Persist) {
                ++metrics_.memory_eviction_persists;
                write_to_storage(victim_id, context);
            } else {
                ++metrics_.memory_eviction_drops;
            }
        }

        metrics_.memory_removed(victim.on_storage);
        victim.in_memory = false;
        --memory_used_blocks_;
        memory_policy_->on_memory_remove(victim_id);
    }

    defer_storage_prune_ = false;
    prune_segment(deferred_prune_scratch_);
    prune_segment(memory_segment_scratch_);
}

void Simulator::write_to_storage(NodeId node_id, const AccessContext& context) {
    Node& node = tree_.node(node_id);
    const Placement placement = placement_policy_->place(
        node,
        context,
        tree_,
        storage_.summary()
    );
    StorageTierState& tier = storage_.tier(placement.tier);

    if (tier.full()) {
        evict_from_storage(placement.tier, node_id, context);
    }

    const std::uint64_t address = tier.allocate();
    node.set_storage_location(StorageLocation{placement.tier, address, placement.stream_id});
    storage_eviction_policy_->on_storage_write(node_id, placement.tier);
    metrics_.storage_written(placement.tier, node.in_memory);
    metrics_.record_io(Operation::Write, placement.tier, placement.stream_id);
    trace_writer_.emit(
        context,
        node_id,
        Operation::Write,
        node,
        node.storage_location(),
        TraceReason::MemoryEviction
    );
}

void Simulator::evict_from_storage(
    StorageTier tier,
    NodeId incoming_node,
    const AccessContext& context
) {
    const NodeId endpoint = storage_eviction_policy_->choose_victim(
        tier,
        incoming_node,
        context,
        tree_
    );
    const StorageEvictionAction action = storage_eviction_policy_->eviction_action(
        tier,
        endpoint,
        incoming_node,
        context,
        tree_
    );
    std::vector<NodeId>& segment = storage_segment_scratch_[storage_tier_index(tier)];
    tree_.resolve_segment(endpoint, segment);
    const std::size_t index = storage_tier_index(tier);
    ++metrics_.storage_evicted_segments[index];
    if (action == StorageEvictionAction::DemoteToTlc) {
        ++metrics_.storage_demoted_segments[index];
    }

    for (NodeId victim_id : segment) {
        Node& victim = tree_.node(victim_id);
        if (!victim.on_storage || victim.storage_tier != tier) {
            continue;
        }
        ++metrics_.storage_evicted_blocks[index];
        if (action == StorageEvictionAction::DemoteToTlc) {
            ++metrics_.storage_demoted_blocks[index];
            demote_to_tlc(victim_id, context);
        } else {
            trim_from_storage(victim_id, context);
        }
    }

    if (defer_storage_prune_) {
        deferred_prune_scratch_.insert(
            deferred_prune_scratch_.end(),
            segment.begin(),
            segment.end()
        );
    } else {
        prune_segment(segment);
    }
}

void Simulator::demote_to_tlc(NodeId node_id, const AccessContext& context) {
    Node& node = tree_.node(node_id);
    const StorageLocation source = node.storage_location();
    const Placement placement = placement_policy_->place_on_tier(
        StorageTier::Tlc,
        node,
        context,
        tree_,
        storage_.summary()
    );
    StorageTierState& target = storage_.tier(placement.tier);

    if (target.full()) {
        evict_from_storage(placement.tier, node_id, context);
    }

    const StorageLocation destination{
        placement.tier,
        target.allocate(),
        placement.stream_id,
    };
    trace_writer_.emit(
        context,
        node_id,
        Operation::Write,
        node,
        destination,
        TraceReason::SlcDemotion
    );
    metrics_.record_io(Operation::Write, destination.tier, destination.stream_id);
    trace_writer_.emit(
        context,
        node_id,
        Operation::Trim,
        node,
        source,
        TraceReason::SlcDemotion
    );
    metrics_.record_io(Operation::Trim, source.tier, source.stream_id);

    storage_eviction_policy_->on_storage_remove(node_id, source.tier);
    storage_.tier(source.tier).release(source.block_address);
    metrics_.storage_removed(source.tier, node.in_memory);
    node.set_storage_location(destination);
    storage_eviction_policy_->on_storage_write(node_id, destination.tier);
    metrics_.storage_written(destination.tier, node.in_memory);
}

void Simulator::trim_from_storage(NodeId node_id, const AccessContext& context) {
    Node& node = tree_.node(node_id);
    const StorageLocation location = node.storage_location();
    trace_writer_.emit(
        context,
        node_id,
        Operation::Trim,
        node,
        location,
        TraceReason::StorageEviction
    );
    metrics_.record_io(Operation::Trim, location.tier, location.stream_id);
    storage_eviction_policy_->on_storage_remove(node_id, location.tier);
    storage_.tier(location.tier).release(location.block_address);
    metrics_.storage_removed(location.tier, node.in_memory);
    node.clear_storage_location();
}

void Simulator::prune_segment(const std::vector<NodeId>& segment) {
    for (NodeId node_id : segment) {
        if (tree_.contains(node_id)) {
            prune_from(node_id);
        }
    }
}

void Simulator::prune_from(NodeId node_id) {
    std::optional<NodeId> current = node_id;
    while (current.has_value() && tree_.contains(*current)) {
        if (active_node_id_ == current) {
            return;
        }
        const Node& node = tree_.node(*current);
        if (node.in_memory || node.on_storage || tree_.child_count(*current) != 0) {
            return;
        }

        const NodeId removed_id = *current;
        const std::optional<NodeId> parent_id = tree_.detach_leaf(removed_id);
        ++metrics_.tree_nodes_removed;
        notify_node_removed(removed_id, parent_id);
        tree_.release_detached(removed_id);
        current = parent_id;
    }
}

void Simulator::notify_node_created(NodeId node_id, std::optional<NodeId> parent_id) {
    memory_policy_->on_node_created(node_id, parent_id, tree_);
    placement_policy_->on_node_created(node_id, parent_id, tree_);
    storage_eviction_policy_->on_node_created(node_id, parent_id, tree_);
}

void Simulator::notify_node_removed(NodeId node_id, std::optional<NodeId> parent_id) {
    memory_policy_->on_node_removed(node_id, parent_id, tree_);
    placement_policy_->on_node_removed(node_id, parent_id, tree_);
    storage_eviction_policy_->on_node_removed(node_id, parent_id, tree_);
}

void Simulator::notify_access_complete(
    const AccessContext& context,
    AccessResult result
) {
    memory_policy_->on_access_complete(context, result, tree_);
    placement_policy_->on_access_complete(context, result, tree_);
    storage_eviction_policy_->on_access_complete(context, result, tree_);
}

}  // namespace dwpdsim
