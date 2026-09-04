#include "dwpdsim/simulator.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <utility>

namespace dwpdsim {
namespace {

NodeSpan span(const std::vector<NodeId>& nodes) {
    return NodeSpan{nodes.data(), nodes.size()};
}

TraceContext trace_context(const AccessContext& access) {
    return TraceContext{
        access.request.timestamp_ns,
        access.request.request_id,
        access.access_sequence,
    };
}

TraceReason relocation_reason(RelocationCause cause) {
    switch (cause) {
        case RelocationCause::Capacity:
            return TraceReason::CapacityEviction;
        case RelocationCause::Access:
            return TraceReason::AccessMigration;
        case RelocationCause::Background:
            return TraceReason::BackgroundMigration;
    }
    return TraceReason::BackgroundMigration;
}

CapacityCause relocation_capacity_cause(RelocationCause cause) {
    switch (cause) {
        case RelocationCause::Access:
            return CapacityCause::AccessMigration;
        case RelocationCause::Background:
            return CapacityCause::BackgroundMigration;
        case RelocationCause::Capacity:
            return CapacityCause::DumpAdmission;
    }
    return CapacityCause::DumpAdmission;
}

void add_counts(
    SegmentBlockByteCounters& counters,
    std::uint64_t blocks,
    std::uint64_t block_size_bytes
) {
    ++counters.segments;
    counters.blocks += blocks;
    counters.bytes += blocks * block_size_bytes;
}

}  // namespace

Simulator::Simulator(
    SimulationConfig config,
    std::unique_ptr<MemoryPolicy> memory_policy,
    std::unique_ptr<StoragePolicy> storage_policy,
    const std::filesystem::path& trace_path
)
    : config_(validate_config(std::move(config))),
      memory_capacity_blocks_(config_.memory.capacity_bytes / config_.block_size_bytes),
      storage_(config_),
      memory_policy_(std::move(memory_policy)),
      storage_policy_(std::move(storage_policy)),
      metrics_(config_),
      trace_writer_(trace_path, config_.block_size_bytes) {
    next_background_tick_ns_ = storage_policy_->background_schedule().period_ns;
}

void Simulator::process_request(
    TimestampNs timestamp_ns,
    RequestId request_id,
    AffinityId affinity_id,
    const HashId* hash_ids,
    std::size_t hash_count
) {
    if (finished_) {
        throw std::logic_error("cannot process requests after finish");
    }
    if (last_timestamp_ns_.has_value() && timestamp_ns < *last_timestamp_ns_) {
        throw std::invalid_argument("request timestamp_ns moved backwards");
    }
    if (!request_ids_.insert(request_id).second) {
        throw std::invalid_argument("request_id must be unique");
    }

    run_until(timestamp_ns);
    collect_protected_prefix(hash_ids, hash_count, protected_prefix_);
    const RequestContext request{
        timestamp_ns,
        request_id,
        affinity_id,
        HashSpan{hash_ids, hash_count},
        span(protected_prefix_),
    };
    storage_policy_->on_request_begin(request, storage_view());
    metrics_.record_request(timestamp_ns);
    last_timestamp_ns_ = timestamp_ns;

    std::optional<NodeId> parent_id;
    for (std::size_t position = 0; position < hash_count; ++position) {
        const auto [node_id, created] = parent_id.has_value()
                                            ? tree_.get_or_create(
                                                  *parent_id,
                                                  hash_ids[position],
                                                  timestamp_ns
                                              )
                                            : tree_.get_or_create_root(
                                                  hash_ids[position],
                                                  timestamp_ns
                                              );
        if (created) {
            ++metrics_.tree_nodes_created;
        }
        process_access(AccessContext{
            request,
            next_access_sequence_++,
            static_cast<std::uint64_t>(position),
            node_id,
            parent_id,
        });
        parent_id = node_id;
    }
}

void Simulator::process_request(
    TimestampNs timestamp_ns,
    RequestId request_id,
    AffinityId affinity_id,
    const std::vector<HashId>& hash_ids
) {
    process_request(
        timestamp_ns,
        request_id,
        affinity_id,
        hash_ids.data(),
        hash_ids.size()
    );
}

void Simulator::finish() {
    const TimestampNs end_ns = config_.simulation_end_ns.value_or(
        last_timestamp_ns_.value_or(0)
    );
    finish(end_ns);
}

void Simulator::finish(TimestampNs simulation_end_ns) {
    if (finished_) {
        return;
    }
    if (last_timestamp_ns_.has_value() && simulation_end_ns < *last_timestamp_ns_) {
        throw std::invalid_argument("simulation_end_ns precedes the last request");
    }
    run_until(simulation_end_ns);
    trace_writer_.finish();
    finished_ = true;
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

StoragePolicyStats Simulator::storage_policy_stats() const {
    return storage_policy_->stats(storage_view());
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
        config.memory.capacity_bytes < config.block_size_bytes ||
        config.memory.capacity_bytes % config.block_size_bytes != 0
    ) {
        throw std::invalid_argument("memory capacity must contain whole blocks");
    }
    for (const StorageTierConfig* tier : {&config.slc, &config.tlc}) {
        if (
            tier->capacity_bytes < config.block_size_bytes ||
            tier->capacity_bytes % config.block_size_bytes != 0
        ) {
            throw std::invalid_argument("storage capacity must contain whole blocks");
        }
        if (tier->stream_count == 0) {
            throw std::invalid_argument("stream_count must be positive");
        }
    }
    if (
        static_cast<std::uint64_t>(config.slc.stream_count) +
        config.tlc.stream_count > 8
    ) {
        throw std::invalid_argument(
            "SLC and TLC stream counts must total at most 8"
        );
    }
    return config;
}

StorageView Simulator::storage_view() const {
    return StorageView{tree_, storage_, config_.block_size_bytes};
}

void Simulator::collect_protected_prefix(
    const HashId* hash_ids,
    std::size_t hash_count,
    std::vector<NodeId>& protected_prefix
) const {
    protected_prefix.clear();
    std::optional<NodeId> parent_id;
    for (std::size_t index = 0; index < hash_count; ++index) {
        const std::optional<NodeId> node_id = parent_id.has_value()
                                                  ? tree_.find_child(
                                                        *parent_id,
                                                        hash_ids[index]
                                                    )
                                                  : tree_.find_root_child(hash_ids[index]);
        if (!node_id.has_value()) {
            break;
        }
        const Node& node = tree_.node(*node_id);
        if (!node.in_memory && !node.on_storage) {
            break;
        }
        protected_prefix.push_back(*node_id);
        parent_id = node_id;
    }
}

void Simulator::run_until(TimestampNs target_ns) {
    const TimestampNs period_ns = storage_policy_->background_schedule().period_ns;
    if (period_ns == 0) {
        return;
    }
    while (next_background_tick_ns_ <= target_ns) {
        drain_background_tick(next_background_tick_ns_);
        next_background_tick_ns_ += period_ns;
    }
}

void Simulator::drain_background_tick(TimestampNs timestamp_ns) {
    ++metrics_.background_ticks;
    const TraceContext context{timestamp_ns, std::nullopt, std::nullopt};
    const std::vector<NodeId> no_protection;
    while (true) {
        const auto action = storage_policy_->next_background_action(
            BackgroundTickContext{timestamp_ns},
            storage_view()
        );
        if (!action.has_value()) {
            return;
        }
        if (!execute_action(*action, ActionPlane::Background, no_protection, context)) {
            return;
        }
    }
}

void Simulator::process_access(const AccessContext& context) {
    active_node_id_ = context.node_id;
    Node& node = tree_.node(context.node_id);
    AccessResult result;

    if (node.in_memory) {
        result = AccessResult::MemoryHit;
        tree_.record_access(context.node_id, context.request.timestamp_ns, true);
        memory_policy_->on_commit(MemoryMutation{
            MemoryMutationKind::Accessed,
            context.node_id,
        });
    } else if (node.on_storage) {
        const StorageLocation source = node.storage_location();
        result = source.tier == StorageTier::Slc ? AccessResult::SlcHit
                                                 : AccessResult::TlcHit;
        tree_.record_access(context.node_id, context.request.timestamp_ns, true);

        const NodeId endpoint = tree_.segment_leaf_for(context.node_id);
        const std::vector<NodeId> accessed_segment_nodes =
            storage_view().resident_nodes(endpoint, source.tier);
        notify_storage_commit(StorageMutation{
            StorageMutationKind::StorageAccessCommitted,
            context.request.timestamp_ns,
            context.request.affinity_id,
            endpoint,
            span(accessed_segment_nodes),
            Placement{source.tier, source.stream_id},
            source.tier,
            0,
        });

        const auto action = storage_policy_->on_storage_access(
            StorageAccessContext{context, source},
            storage_view()
        );
        bool relocated = false;
        if (action.has_value() && std::holds_alternative<RelocateIntent>(*action)) {
            const RelocateIntent& intent = std::get<RelocateIntent>(*action);
            std::vector<NodeId> protected_nodes(
                context.request.protected_prefix.begin(),
                context.request.protected_prefix.end()
            );
            const SegmentView source_segment = storage_view().resolve_segment(
                intent.source_segment_endpoint
            );
            protected_nodes.insert(
                protected_nodes.end(),
                source_segment.ordered_nodes.begin(),
                source_segment.ordered_nodes.end()
            );
            const std::uint64_t source_blocks = storage_view().resident_blocks(
                intent.source_segment_endpoint,
                source.tier
            );
            if (ensure_capacity(
                    intent.destination.tier,
                    source_blocks,
                    CapacityCause::AccessMigration,
                    protected_nodes,
                    trace_context(context)
                )) {
                const MoveId move_id = next_move_id_++;
                const std::uint64_t read_sequence = trace_writer_.emit(
                    trace_context(context),
                    context.node_id,
                    Operation::Read,
                    node,
                    source,
                    TraceReason::StorageHit,
                    move_id
                );
                metrics_.record_io(Operation::Read, source.tier, source.stream_id);
                relocated = execute_relocation(
                    intent,
                    protected_nodes,
                    trace_context(context),
                    context.node_id,
                    read_sequence,
                    true,
                    move_id
                );
            }
        }
        if (!relocated) {
            trace_writer_.emit(
                trace_context(context),
                context.node_id,
                Operation::Read,
                node,
                source,
                TraceReason::StorageHit
            );
            metrics_.record_io(Operation::Read, source.tier, source.stream_id);
        }

        if (memory_policy_->admit_storage_hit(context, node, tree_)) {
            ++metrics_.storage_promotions;
            insert_into_memory(context.node_id, context);
        } else {
            ++metrics_.storage_bypasses;
        }
    } else {
        result = AccessResult::GlobalMiss;
        tree_.record_access(context.node_id, context.request.timestamp_ns, false);
        insert_into_memory(context.node_id, context);
    }

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
    memory_policy_->on_commit(MemoryMutation{MemoryMutationKind::Inserted, node_id});
    metrics_.memory_inserted(node.on_storage);
}

void Simulator::evict_from_memory(const AccessContext& context) {
    const MemoryEvictionDecision decision = memory_policy_->evict(context.request, tree_);
    std::vector<NodeId> memory_nodes;
    std::vector<NodeId> write_nodes;
    std::optional<NodeId> endpoint = decision.leaf_segment_endpoint;
    while (endpoint.has_value()) {
        tree_.resolve_segment(*endpoint, memory_segment_scratch_);
        const SegmentView segment{
            tree_.segment_top(*endpoint),
            *endpoint,
            memory_segment_scratch_,
        };
        const std::optional<NodeId> parent_segment =
            decision.action == MemoryEvictionAction::Dump
                ? tree_.parent(segment.segment_top)
                : std::nullopt;

        memory_nodes.clear();
        write_nodes.clear();
        for (NodeId node_id : segment.ordered_nodes) {
            const Node& node = tree_.node(node_id);
            if (!node.in_memory) {
                continue;
            }
            memory_nodes.push_back(node_id);
            if (!node.on_storage) {
                write_nodes.push_back(node_id);
            } else {
                ++metrics_.memory_evictions_with_storage_copy;
            }
        }

        if (memory_nodes.empty()) {
            if (decision.action == MemoryEvictionAction::Drop) {
                return;
            }
            endpoint = parent_segment;
            continue;
        }

        ++metrics_.memory_evicted_segments;
        metrics_.memory_evicted_blocks += memory_nodes.size();
        const bool has_unwritten_blocks = !write_nodes.empty();
        if (decision.action == MemoryEvictionAction::Drop) {
            ++metrics_.memory_drop_segments;
            metrics_.memory_drop_blocks += memory_nodes.size();
        } else if (has_unwritten_blocks) {
            ++metrics_.memory_dump_segments;
            metrics_.memory_dump_blocks += write_nodes.size();
            if (!dump_segment(context, segment, write_nodes)) {
                ++metrics_.memory_drop_segments;
                metrics_.memory_drop_blocks += memory_nodes.size();
            }
        }

        for (NodeId node_id : memory_nodes) {
            Node& victim = tree_.node(node_id);
            metrics_.memory_removed(victim.on_storage);
            victim.in_memory = false;
            --memory_used_blocks_;
            memory_policy_->on_commit(
                MemoryMutation{MemoryMutationKind::Removed, node_id}
            );
        }
        prune_segment(segment.ordered_nodes);
        if (decision.action == MemoryEvictionAction::Drop || has_unwritten_blocks) {
            return;
        }
        endpoint = parent_segment;
    }
}

bool Simulator::dump_segment(
    const AccessContext& context,
    const SegmentView& segment,
    const std::vector<NodeId>& write_nodes
) {
    ++metrics_.dump_requests;
    const std::uint64_t write_blocks = write_nodes.size();
    const std::uint64_t write_bytes = write_blocks * config_.block_size_bytes;
    std::vector<NodeId> protected_nodes(
        context.request.protected_prefix.begin(),
        context.request.protected_prefix.end()
    );
    protected_nodes.insert(
        protected_nodes.end(),
        segment.ordered_nodes.begin(),
        segment.ordered_nodes.end()
    );
    const DumpContext dump{
        context.request,
        segment,
        span(write_nodes),
        write_blocks,
        write_bytes,
        span(protected_nodes),
    };
    const DumpPlacementDecision decision = storage_policy_->place_dump(dump, storage_view());
    if (!ensure_capacity(
            decision.placement.tier,
            write_blocks,
            CapacityCause::DumpAdmission,
            protected_nodes,
            trace_context(context)
        )) {
        ++metrics_.admission_rejections;
        add_counts(metrics_.dumps_rejected, write_blocks, config_.block_size_bytes);
        return false;
    }

    StorageTierState& tier = storage_.tier(decision.placement.tier);
    for (NodeId node_id : write_nodes) {
        Node& node = tree_.node(node_id);
        const StorageLocation location{
            decision.placement.tier,
            tier.allocate(),
            decision.placement.stream_id,
        };
        node.set_storage_location(location);
        metrics_.storage_written(location.tier, node.in_memory);
        metrics_.record_io(Operation::Write, location.tier, location.stream_id, true);
        trace_writer_.emit(
            trace_context(context),
            node_id,
            Operation::Write,
            node,
            location,
            TraceReason::MemoryDump
        );
    }
    notify_storage_commit(StorageMutation{
        StorageMutationKind::DumpWriteCommitted,
        context.request.timestamp_ns,
        context.request.affinity_id,
        segment.segment_endpoint,
        span(write_nodes),
        decision.placement,
        decision.placement.tier,
        write_bytes,
    });
    add_counts(metrics_.dumps_admitted, write_blocks, config_.block_size_bytes);
    add_counts(
        metrics_.placements[storage_tier_index(decision.placement.tier)],
        write_blocks,
        config_.block_size_bytes
    );
    return true;
}

bool Simulator::ensure_capacity(
    StorageTier tier,
    std::uint64_t required_blocks,
    CapacityCause cause,
    const std::vector<NodeId>& protected_nodes,
    const TraceContext& trace_context_value
) {
    const std::uint64_t limit = storage_policy_->capacity_limit_blocks(tier, storage_view());
    if (required_blocks > limit) {
        ++metrics_.no_space;
        return false;
    }
    while (storage_.tier(tier).used_blocks() + required_blocks > limit) {
        const CapacityPressureContext pressure{
            trace_context_value.timestamp_ns,
            cause,
            tier,
            required_blocks,
            span(protected_nodes),
        };
        const auto action = storage_policy_->reclaim_for(pressure, storage_view());
        if (!action.has_value() ||
            !execute_action(
                *action,
                ActionPlane::Capacity,
                protected_nodes,
                trace_context_value
            )) {
            ++metrics_.no_space;
            const bool protected_target = std::any_of(
                protected_nodes.begin(),
                protected_nodes.end(),
                [this, tier](NodeId node_id) {
                    if (!tree_.contains(node_id)) {
                        return false;
                    }
                    const Node& node = tree_.node(node_id);
                    return node.on_storage && node.storage_tier == tier;
                }
            );
            if (protected_target) {
                ++metrics_.protected_victim_exhaustion;
            }
            return false;
        }
    }
    return true;
}

bool Simulator::execute_action(
    const StorageActionIntent& action,
    ActionPlane plane,
    const std::vector<NodeId>& protected_nodes,
    const TraceContext& trace_context_value
) {
    if (std::holds_alternative<TrimIntent>(action)) {
        return execute_trim(
            std::get<TrimIntent>(action),
            plane,
            trace_context_value
        );
    }
    return execute_relocation(
        std::get<RelocateIntent>(action),
        protected_nodes,
        trace_context_value
    );
}

bool Simulator::execute_trim(
    const TrimIntent& intent,
    ActionPlane plane,
    const TraceContext& trace_context_value
) {
    const std::vector<NodeId> nodes = storage_view().resident_nodes(
        intent.segment_endpoint,
        intent.tier
    );
    if (nodes.empty()) {
        return false;
    }
    const TraceReason reason = plane == ActionPlane::Capacity
                                   ? TraceReason::CapacityEviction
                                   : TraceReason::IdleEviction;
    for (NodeId node_id : nodes) {
        Node& node = tree_.node(node_id);
        const StorageLocation location = node.storage_location();
        trace_writer_.emit(
            trace_context_value,
            node_id,
            Operation::Trim,
            node,
            location,
            reason
        );
        metrics_.record_io(Operation::Trim, location.tier, location.stream_id);
        storage_.tier(location.tier).release(location.block_address);
        metrics_.storage_removed(location.tier, node.in_memory);
        node.clear_storage_location();
    }
    notify_storage_commit(StorageMutation{
        plane == ActionPlane::Capacity
            ? StorageMutationKind::CapacityTrimCommitted
            : StorageMutationKind::IdleTrimCommitted,
        trace_context_value.timestamp_ns,
        0,
        intent.segment_endpoint,
        span(nodes),
        Placement{intent.tier, 0},
        intent.tier,
        nodes.size() * config_.block_size_bytes,
    });
    if (plane == ActionPlane::Capacity) {
        add_counts(
            metrics_.foreground_capacity_evictions,
            nodes.size(),
            config_.block_size_bytes
        );
    } else {
        add_counts(
            metrics_.background_idle_evictions,
            nodes.size(),
            config_.block_size_bytes
        );
    }
    prune_segment(nodes);
    return true;
}

bool Simulator::execute_relocation(
    const RelocateIntent& intent,
    const std::vector<NodeId>& protected_nodes,
    const TraceContext& trace_context_value,
    std::optional<NodeId> reused_read_node,
    std::optional<std::uint64_t> reused_read_sequence,
    bool capacity_ready,
    std::optional<MoveId> assigned_move_id
) {
    const SegmentView segment = storage_view().resolve_segment(
        intent.source_segment_endpoint
    );
    std::optional<StorageTier> source_tier;
    for (NodeId node_id : segment.ordered_nodes) {
        const Node& node = tree_.node(node_id);
        if (node.on_storage && node.storage_tier != intent.destination.tier) {
            source_tier = node.storage_tier;
            break;
        }
    }
    if (!source_tier.has_value()) {
        return false;
    }
    const std::vector<NodeId> nodes = storage_view().resident_nodes(
        intent.source_segment_endpoint,
        *source_tier
    );
    if (nodes.empty()) {
        return false;
    }

    std::vector<NodeId> combined_protection = protected_nodes;
    combined_protection.insert(
        combined_protection.end(),
        segment.ordered_nodes.begin(),
        segment.ordered_nodes.end()
    );
    if (!capacity_ready &&
        !ensure_capacity(
            intent.destination.tier,
            nodes.size(),
            relocation_capacity_cause(intent.cause),
            combined_protection,
            trace_context_value
        )) {
        return false;
    }

    const MoveId move_id = assigned_move_id.has_value()
                               ? *assigned_move_id
                               : next_move_id_++;
    const TraceReason reason = relocation_reason(intent.cause);
    std::vector<StorageLocation> sources;
    std::vector<StorageLocation> destinations;
    std::vector<std::uint64_t> read_sequences;
    std::vector<std::uint64_t> write_sequences;
    sources.reserve(nodes.size());
    destinations.reserve(nodes.size());
    read_sequences.reserve(nodes.size());
    write_sequences.reserve(nodes.size());

    StorageTierState& destination_tier = storage_.tier(intent.destination.tier);
    for (NodeId node_id : nodes) {
        sources.push_back(tree_.node(node_id).storage_location());
        destinations.push_back(StorageLocation{
            intent.destination.tier,
            destination_tier.allocate(),
            intent.destination.stream_id,
        });
    }

    for (std::size_t index = 0; index < nodes.size(); ++index) {
        const NodeId node_id = nodes[index];
        if (reused_read_node == node_id) {
            read_sequences.push_back(*reused_read_sequence);
            ++metrics_.relocation_reused_read_blocks;
            metrics_.relocation_reused_read_bytes += config_.block_size_bytes;
        } else {
            read_sequences.push_back(trace_writer_.emit(
                trace_context_value,
                node_id,
                Operation::Read,
                tree_.node(node_id),
                sources[index],
                reason,
                move_id
            ));
            metrics_.record_io(
                Operation::Read,
                sources[index].tier,
                sources[index].stream_id
            );
            ++metrics_.relocation_explicit_read_blocks;
            metrics_.relocation_explicit_read_bytes += config_.block_size_bytes;
        }
    }
    metrics_.relocation_source_read_blocks += nodes.size();
    metrics_.relocation_source_read_bytes += nodes.size() * config_.block_size_bytes;
    notify_storage_commit(StorageMutation{
        StorageMutationKind::RelocationReadCommitted,
        trace_context_value.timestamp_ns,
        0,
        intent.source_segment_endpoint,
        span(nodes),
        intent.destination,
        *source_tier,
        nodes.size() * config_.block_size_bytes,
    });

    for (std::size_t index = 0; index < nodes.size(); ++index) {
        Node& node = tree_.node(nodes[index]);
        write_sequences.push_back(trace_writer_.emit(
            trace_context_value,
            nodes[index],
            Operation::Write,
            node,
            destinations[index],
            reason,
            move_id,
            read_sequences[index]
        ));
        metrics_.record_io(
            Operation::Write,
            destinations[index].tier,
            destinations[index].stream_id
        );
        metrics_.storage_removed(sources[index].tier, node.in_memory);
        node.set_storage_location(destinations[index]);
        metrics_.storage_written(destinations[index].tier, node.in_memory);
    }
    metrics_.relocation_destination_write_blocks += nodes.size();
    metrics_.relocation_destination_write_bytes += nodes.size() * config_.block_size_bytes;
    notify_storage_commit(StorageMutation{
        StorageMutationKind::RelocationWriteCommitted,
        trace_context_value.timestamp_ns,
        0,
        intent.source_segment_endpoint,
        span(nodes),
        intent.destination,
        *source_tier,
        nodes.size() * config_.block_size_bytes,
    });

    for (std::size_t index = 0; index < nodes.size(); ++index) {
        trace_writer_.emit(
            trace_context_value,
            nodes[index],
            Operation::Trim,
            tree_.node(nodes[index]),
            sources[index],
            reason,
            move_id,
            write_sequences[index]
        );
        metrics_.record_io(
            Operation::Trim,
            sources[index].tier,
            sources[index].stream_id
        );
        storage_.tier(sources[index].tier).release(sources[index].block_address);
    }
    metrics_.relocation_source_trim_blocks += nodes.size();
    metrics_.relocation_source_trim_bytes += nodes.size() * config_.block_size_bytes;
    notify_storage_commit(StorageMutation{
        StorageMutationKind::RelocationSourceTrimCommitted,
        trace_context_value.timestamp_ns,
        0,
        intent.source_segment_endpoint,
        span(nodes),
        intent.destination,
        *source_tier,
        nodes.size() * config_.block_size_bytes,
    });

    SegmentBlockByteCounters* migration = nullptr;
    switch (intent.cause) {
        case RelocationCause::Access:
            migration = &metrics_.access_migrations;
            break;
        case RelocationCause::Background:
            migration = &metrics_.background_migrations;
            break;
        case RelocationCause::Capacity:
            migration = &metrics_.capacity_migrations;
            add_counts(
                metrics_.foreground_capacity_evictions,
                nodes.size(),
                config_.block_size_bytes
            );
            break;
    }
    add_counts(*migration, nodes.size(), config_.block_size_bytes);
    return true;
}

void Simulator::prune_segment(const std::vector<NodeId>& segment) {
    for (auto node = segment.rbegin(); node != segment.rend(); ++node) {
        if (tree_.contains(*node)) {
            prune_from(*node);
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
        const std::vector<NodeId> removed{removed_id};
        notify_storage_commit(StorageMutation{
            StorageMutationKind::NodePruned,
            last_timestamp_ns_.value_or(0),
            0,
            removed_id,
            span(removed),
            {},
            StorageTier::Slc,
            0,
        });
        tree_.release_detached(removed_id);
        current = parent_id;
    }
}

void Simulator::notify_storage_commit(const StorageMutation& mutation) {
    storage_policy_->on_commit(mutation, storage_view());
}

}  // namespace dwpdsim
