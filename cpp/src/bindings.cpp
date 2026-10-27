#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <limits>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "dwpdsim/config.hpp"
#include "dwpdsim/metrics.hpp"
#include "dwpdsim/policies.hpp"
#include "dwpdsim/simulator.hpp"
#include "dwpdsim/types.hpp"

namespace py = pybind11;

namespace dwpdsim {
namespace {

py::buffer_info require_u64_buffer(const py::buffer& buffer, const char* name) {
    py::buffer_info info = buffer.request();
    const bool unsigned_64_format =
        info.format == "L" || info.format == "Q" || info.format == "@L" ||
        info.format == "@Q" || info.format == "=L" || info.format == "=Q";
    if (
        info.ndim != 1 ||
        info.itemsize != static_cast<py::ssize_t>(sizeof(std::uint64_t)) ||
        !unsigned_64_format ||
        info.strides[0] != static_cast<py::ssize_t>(sizeof(std::uint64_t))
    ) {
        throw py::value_error(std::string(name) + " must be a contiguous uint64 buffer");
    }
    return info;
}

StorageTier parse_storage_tier(const std::string& value) {
    if (value == "slc") {
        return StorageTier::Slc;
    }
    if (value == "tlc") {
        return StorageTier::Tlc;
    }
    throw py::value_error("fixed_tier must be 'slc' or 'tlc'");
}

std::unique_ptr<StoragePolicy> make_storage_policy(
    const std::string& kind,
    const SimulationConfig& simulation,
    const std::string& fixed_tier,
    std::uint32_t fixed_stream_id,
    double slc_write_ratio,
    const WearShareRoundRobinPolicyConfig& round_robin,
    const WearShareAffinityPolicyConfig& affinity,
    const AdaptiveEndurancePolicyConfig& adaptive,
    const WearBalancedPolicyConfig& wear_balanced
) {
    if (kind == "infinite_storage") { return nullptr; }
    if (simulation.hybrid_attention.enabled && kind != "wear_share_round_robin" &&
        kind != "adaptive_endurance") {
        throw py::value_error("finite hybrid storage supports wear_share_round_robin and adaptive_endurance");
    }
    if (kind == "baseline_fixed_lru") {
        const StorageTier tier = parse_storage_tier(fixed_tier);
        if (fixed_stream_id >= simulation.slc.stream_count && tier == StorageTier::Slc) {
            throw py::value_error("fixed_stream_id is outside the SLC tier");
        }
        if (fixed_stream_id >= simulation.tlc.stream_count && tier == StorageTier::Tlc) {
            throw py::value_error("fixed_stream_id is outside the TLC tier");
        }
        return std::make_unique<BaselineFixedLruStoragePolicy>(
            Placement{tier, fixed_stream_id}
        );
    }
    if (kind == "baseline_ratio_lru") {
        if (slc_write_ratio < 0.0 || slc_write_ratio > 1.0) {
            throw py::value_error("slc_write_ratio must be between 0 and 1");
        }
        return std::make_unique<BaselineRatioLruStoragePolicy>(slc_write_ratio);
    }
    if (kind == "wear_share_round_robin") {
        return std::make_unique<WearShareRoundRobinStoragePolicy>(round_robin);
    }
    if (kind == "wear_share_affinity") {
        return std::make_unique<WearShareAffinityStoragePolicy>(affinity);
    }
    if (kind == "wear_balanced") {
        return std::make_unique<WearBalancedStoragePolicy>(wear_balanced);
    }
    if (kind == "adaptive_endurance") {
        return std::make_unique<AdaptiveEnduranceStoragePolicy>(adaptive);
    }
    throw py::value_error(
        "storage policy must be baseline_fixed_lru, baseline_ratio_lru, "
        "wear_share_round_robin, wear_share_affinity, adaptive_endurance, wear_balanced, or infinite_storage"
    );
}

py::dict blocks_and_bytes(std::uint64_t blocks, std::uint64_t block_size_bytes) {
    py::dict result;
    result["blocks"] = blocks;
    result["bytes"] = blocks * block_size_bytes;
    return result;
}

py::dict counts(const SegmentBlockByteCounters& counters) {
    py::dict result;
    result["segments"] = counters.segments;
    result["blocks"] = counters.blocks;
    result["bytes"] = counters.bytes;
    return result;
}

double rate(std::uint64_t numerator, std::uint64_t denominator) {
    return denominator == 0 ? 0.0 : static_cast<double>(numerator) / denominator;
}

py::dict storage_tier_metrics(
    const Simulator& simulator,
    StorageTier tier,
    const StorageTierConfig& config
) {
    const MetricsCollector& metrics = simulator.metrics();
    const std::size_t index = storage_tier_index(tier);
    const StorageTierIoCounters& io = metrics.io[index];
    const std::uint64_t block_size = simulator.config().block_size_bytes;

    py::dict streams;
    for (std::size_t stream_id = 0; stream_id < io.stream_writes.size(); ++stream_id) {
        streams[py::str(std::to_string(stream_id))] = blocks_and_bytes(
            io.stream_writes[stream_id],
            block_size
        );
    }

    py::dict result;
    result["capacity_bytes"] = config.capacity_bytes;
    if (simulator.config().infinite_storage && tier == StorageTier::Tlc) {
        result["capacity_bytes"] = py::none();
    }
    result["stream_count"] = config.stream_count;
    result["live_bytes"] = metrics.storage_resident_blocks[index] * block_size;
    result["peak_live_bytes"] = metrics.peak_storage_resident_blocks[index] * block_size;
    result["host_write_bytes"] = io.host_writes * block_size;
    result["program_bytes"] = io.writes * block_size;
    result["reads"] = blocks_and_bytes(io.reads, block_size);
    result["writes"] = blocks_and_bytes(io.writes, block_size);
    result["trims"] = blocks_and_bytes(io.trims, block_size);
    result["stream_writes"] = std::move(streams);
    return result;
}

py::dict simulator_stats(const Simulator& simulator) {
    const SimulationConfig& config = simulator.config();
    const MetricsCollector& metrics = simulator.metrics();
    const StoragePolicyStats policy = simulator.storage_policy_stats();
    const std::uint64_t block_size = config.block_size_bytes;
    const std::uint64_t storage_hits = metrics.slc_hits + metrics.tlc_hits;
    const std::uint64_t memory_misses = storage_hits + metrics.global_misses;
    const std::uint64_t all_hits = metrics.memory_hits + storage_hits;

    py::dict time;
    time["unit"] = "ns";
    if (metrics.start_timestamp_ns.has_value()) {
        time["start_ns"] = *metrics.start_timestamp_ns;
        time["last_request_ns"] = *metrics.end_timestamp_ns;
    } else {
        time["start_ns"] = py::none();
        time["last_request_ns"] = py::none();
    }
    if (config.simulation_end_ns.has_value()) {
        time["simulation_end_ns"] = *config.simulation_end_ns;
    } else {
        time["simulation_end_ns"] = py::none();
    }

    py::dict configuration;
    configuration["block_size_bytes"] = block_size;
    configuration["memory_capacity_bytes"] = config.memory.capacity_bytes;
    configuration["slc_capacity_bytes"] = config.slc.capacity_bytes;
    configuration["tlc_capacity_bytes"] = config.tlc.capacity_bytes;
    configuration["slc_stream_count"] = config.slc.stream_count;
    configuration["tlc_stream_count"] = config.tlc.stream_count;
    if (config.infinite_storage) {
        configuration["storage_mode"] = "infinite_storage";
        configuration["tlc_capacity_bytes"] = py::none();
    }

    py::dict accesses;
    accesses["requests"] = metrics.request_count;
    accesses["total"] = metrics.block_access_count;
    accesses["memory_hits"] = metrics.memory_hits;
    accesses["slc_hits"] = metrics.slc_hits;
    accesses["tlc_hits"] = metrics.tlc_hits;
    accesses["global_misses"] = metrics.global_misses;
    accesses["compute_cost"] = metrics.compute_cost;
    accesses["memory_hit_rate"] = rate(metrics.memory_hits, metrics.block_access_count);
    accesses["storage_hit_rate"] = rate(storage_hits, memory_misses);
    accesses["total_hit_rate"] = rate(all_hits, metrics.block_access_count);
    accesses["global_miss_rate"] = rate(metrics.global_misses, metrics.block_access_count);

    py::dict memory;
    memory["capacity_blocks"] = simulator.memory_capacity_blocks();
    memory["resident_blocks"] = metrics.memory_resident_blocks;
    memory["peak_resident_blocks"] = metrics.peak_memory_resident_blocks;
    memory["storage_promotions"] = metrics.storage_promotions;
    memory["storage_bypasses"] = metrics.storage_bypasses;
    memory["evicted_segments"] = metrics.memory_evicted_segments;
    memory["evicted_blocks"] = metrics.memory_evicted_blocks;
    memory["evictions_with_storage_copy"] = metrics.memory_evictions_with_storage_copy;
    memory["drop_segments"] = metrics.memory_drop_segments;
    memory["drop_blocks"] = metrics.memory_drop_blocks;
    memory["dump_segments"] = metrics.memory_dump_segments;
    memory["dump_blocks"] = metrics.memory_dump_blocks;

    py::dict storage;
    storage["slc"] = storage_tier_metrics(simulator, StorageTier::Slc, config.slc);
    storage["tlc"] = storage_tier_metrics(simulator, StorageTier::Tlc, config.tlc);
    storage["duplicated_blocks"] = metrics.duplicated_blocks;

    py::dict dumps;
    dumps["requests"] = metrics.dump_requests;
    dumps["admitted"] = counts(metrics.dumps_admitted);
    dumps["rejected"] = counts(metrics.dumps_rejected);

    py::dict background;
    background["ticks"] = metrics.background_ticks;
    background["idle_evictions"] = counts(metrics.background_idle_evictions);

    py::dict migrations;
    migrations["access"] = counts(metrics.access_migrations);
    migrations["background"] = counts(metrics.background_migrations);
    migrations["capacity"] = counts(metrics.capacity_migrations);

    py::dict relocation;
    relocation["source_reads"] = blocks_and_bytes(
        metrics.relocation_source_read_blocks,
        block_size
    );
    relocation["reused_access_reads"] = blocks_and_bytes(
        metrics.relocation_reused_read_blocks,
        block_size
    );
    relocation["explicit_reads"] = blocks_and_bytes(
        metrics.relocation_explicit_read_blocks,
        block_size
    );
    relocation["destination_writes"] = blocks_and_bytes(
        metrics.relocation_destination_write_blocks,
        block_size
    );
    relocation["source_trims"] = blocks_and_bytes(
        metrics.relocation_source_trim_blocks,
        block_size
    );

    py::dict placement;
    placement["slc"] = counts(metrics.placements[0]);
    placement["tlc"] = counts(metrics.placements[1]);

    py::dict algorithm;
    algorithm["slc_program_bytes"] = policy.slc_program_bytes;
    algorithm["tlc_program_bytes"] = policy.tlc_program_bytes;
    algorithm["gap_samples"] = policy.gap_samples;
    algorithm["gap_q95_seconds"] = policy.gap_q95_seconds;
    algorithm["idle_threshold_seconds"] = policy.idle_threshold_seconds;
    if (policy.wear_balance) {
        const auto& wear = *policy.wear_balance;
        algorithm["slc_wa"] = wear.slc_wa;
        algorithm["tlc_wa"] = wear.tlc_wa;
        algorithm["shadow_slc_pressure"] = wear.shadow_slc_pressure;
        algorithm["shadow_tlc_pressure"] = wear.shadow_tlc_pressure;
        algorithm["learned_idle_multiplier"] = wear.learned_idle_multiplier;
        algorithm["reuse_retention_ema"] = wear.reuse_retention_ema;
        algorithm["feedback_windows"] = wear.feedback_windows;
        algorithm["target_tlc_share"] = wear.target_tlc_share;
        algorithm["effective_promotion_seconds"] = wear.effective_promotion_seconds;
    }

    py::dict errors;
    errors["no_space"] = metrics.no_space;
    errors["protected_victim_exhaustion"] = metrics.protected_victim_exhaustion;
    errors["admission_rejections"] = metrics.admission_rejections;

    py::dict tree;
    tree["nodes"] = simulator.tree().size();
    tree["nodes_created"] = metrics.tree_nodes_created;
    tree["nodes_removed"] = metrics.tree_nodes_removed;

    py::dict trace;
    trace["schema_version"] = 4;
    trace["events"] = simulator.trace_event_count();

    py::dict result;
    result["time"] = std::move(time);
    result["configuration"] = std::move(configuration);
    result["accesses"] = std::move(accesses);
    result["created"] = blocks_and_bytes(metrics.global_misses, block_size);
    result["memory"] = std::move(memory);
    result["storage"] = std::move(storage);
    result["dumps"] = std::move(dumps);
    result["foreground_capacity_evictions"] = counts(
        metrics.foreground_capacity_evictions
    );
    result["background"] = std::move(background);
    result["migrations"] = std::move(migrations);
    result["relocation"] = std::move(relocation);
    result["placement"] = std::move(placement);
    result["algorithm"] = std::move(algorithm);
    result["errors"] = std::move(errors);
    result["tree"] = std::move(tree);
    result["trace"] = std::move(trace);
    if (const auto* shadow = simulator.shadow()) {
        const auto state = shadow->stats();
        py::dict output;
        output["model"] = "shadow_ftl";
        const auto& settings = shadow->config();
        output["page_bytes"] = settings.page_bytes;
        output["pages_per_block"] = settings.pages_per_block;
        output["overprovisioning"] = settings.overprovisioning;
        output["feedback_period_ns"] = settings.feedback_period_ns;
        output["online_tuning"] = settings.online_tuning;
        output["reuse_loss_budget"] = settings.reuse_loss_budget;
        output["min_reuse_blocks"] = settings.min_reuse_blocks;
        output["reuse_ema_scale_blocks"] = settings.reuse_ema_scale_blocks;
        output["slc_endurance"] = settings.slc_endurance;
        output["tlc_endurance"] = settings.tlc_endurance;
        output["ghost_entries"] = shadow->ghost_entries();
        output["ghost_miss_blocks"] = shadow->total_ghost_misses();
        for (std::size_t i = 0; i < 2; ++i) {
            const auto& pool = state.pools[i];
            py::dict values;
            values["host_program_pages"] = pool.host_program_pages;
            values["gc_program_pages"] = pool.gc_program_pages;
            values["erases"] = pool.erases;
            values["physical_blocks"] = pool.physical_blocks;
            values["nominal_bytes"] = pool.nominal_bytes;
            values["wa_ema"] = pool.wa_ema;
            values["lifetime_pressure"] = pool.lifetime_pressure;
            values["write_amplification"] = pool.host_program_pages ?
                static_cast<double>(pool.host_program_pages + pool.gc_program_pages) / pool.host_program_pages : 1.0;
            output[i == 0 ? "slc" : "tlc"] = values;
        }
        result["shadow_ftl"] = output;
    }
    return result;
}

}  // namespace
}  // namespace dwpdsim

PYBIND11_MODULE(_core, module) {
    using namespace dwpdsim;

    module.doc() = "C++ core for DWPDSim";
    module.attr("CORE_VERSION") = "1.0.0";

    py::class_<MemoryConfig>(module, "MemoryConfig")
        .def(py::init<>())
        .def_readwrite("capacity_bytes", &MemoryConfig::capacity_bytes);

    py::class_<StorageTierConfig>(module, "StorageTierConfig")
        .def(py::init<>())
        .def_readwrite("capacity_bytes", &StorageTierConfig::capacity_bytes)
        .def_readwrite("stream_count", &StorageTierConfig::stream_count);

    py::class_<JointCostConfig>(module, "JointCostConfig")
        .def(py::init<>())
        .def_readwrite("horizons_seconds", &JointCostConfig::horizons_seconds)
        .def_readwrite("wear_weight", &JointCostConfig::wear_weight)
        .def_readwrite("nand_seconds_per_gib", &JointCostConfig::nand_seconds_per_gib)
        .def_readwrite("storage_read_bytes_per_second", &JointCostConfig::storage_read_bytes_per_second)
        .def_readwrite("storage_write_bytes_per_second", &JointCostConfig::storage_write_bytes_per_second)
        .def_readwrite("gpu_bytes_per_second", &JointCostConfig::gpu_bytes_per_second)
        .def_readwrite("prefill_tokens_per_second", &JointCostConfig::prefill_tokens_per_second)
        .def_readwrite("candidates_per_kind", &JointCostConfig::candidates_per_kind)
        .def_readwrite("scenario_limit", &JointCostConfig::scenario_limit)
        .def_readwrite("reclaim_bytes", &JointCostConfig::reclaim_bytes)
        .def_readwrite("sample_every", &JointCostConfig::sample_every)
        .def_readwrite("samples", &JointCostConfig::samples)
        .def_readwrite("seed", &JointCostConfig::seed)
        .def_readwrite("nand_page_bytes", &JointCostConfig::nand_page_bytes)
        .def_readwrite("nand_pages_per_block", &JointCostConfig::nand_pages_per_block)
        .def_readwrite("overprovisioning", &JointCostConfig::overprovisioning);

    py::class_<HybridAttentionConfig>(module, "HybridAttentionConfig")
        .def(py::init<>())
        .def_readwrite("enabled", &HybridAttentionConfig::enabled)
        .def_readwrite("full_capacity_bytes", &HybridAttentionConfig::full_capacity_bytes)
        .def_readwrite("linear_capacity_bytes", &HybridAttentionConfig::linear_capacity_bytes)
        .def_readwrite("full_block_bytes", &HybridAttentionConfig::full_block_bytes)
        .def_readwrite("linear_checkpoint_bytes", &HybridAttentionConfig::linear_checkpoint_bytes)
        .def_readwrite("full_policy", &HybridAttentionConfig::full_policy)
        .def_readwrite("linear_policy", &HybridAttentionConfig::linear_policy)
        .def_readwrite("lru_k", &HybridAttentionConfig::lru_k)
        .def_readwrite("alpha", &HybridAttentionConfig::alpha)
        .def_readwrite("decay_seconds", &HybridAttentionConfig::decay_seconds)
        .def_readwrite("linear_read_cost", &HybridAttentionConfig::linear_read_cost)
        .def_readwrite("admit_storage_hits", &HybridAttentionConfig::admit_storage_hits)
        .def_readwrite("dump_on_eviction", &HybridAttentionConfig::dump_on_eviction)
        .def_readwrite("replay_mode", &HybridAttentionConfig::replay_mode)
        .def_readwrite("tokens_per_block", &HybridAttentionConfig::tokens_per_block)
        .def_readwrite("checkpoint_interval_tokens", &HybridAttentionConfig::checkpoint_interval_tokens)
        .def_readwrite("checkpoint_generation", &HybridAttentionConfig::checkpoint_generation)
        .def_readwrite("full_drop_fraction", &HybridAttentionConfig::full_drop_fraction)
        .def_readwrite("linear_drop_fraction", &HybridAttentionConfig::linear_drop_fraction)
        .def_readwrite("emit_io_trace", &HybridAttentionConfig::emit_io_trace)
        .def_readwrite("recompute_weight", &HybridAttentionConfig::recompute_weight)
        .def_readwrite("tail_prior", &HybridAttentionConfig::tail_prior)
        .def_readwrite("drop_phase", &HybridAttentionConfig::drop_phase)
        .def_readwrite("pool_allocation", &HybridAttentionConfig::pool_allocation)
        .def_readwrite("pool_decay_seconds", &HybridAttentionConfig::pool_decay_seconds)
        .def_readwrite("eviction_mode", &HybridAttentionConfig::eviction_mode)
        .def_readwrite("full_retention_ns", &HybridAttentionConfig::full_retention_ns)
        .def_readwrite("linear_retention_ns", &HybridAttentionConfig::linear_retention_ns)
        .def_readwrite("linear_retention_policy", &HybridAttentionConfig::linear_retention_policy)
        .def_readwrite("cost", &HybridAttentionConfig::cost);

    py::class_<HybridPrefillRequest>(module, "HybridPrefillRequest")
        .def(py::init<>())
        .def_readwrite("timestamp_ns", &HybridPrefillRequest::timestamp_ns)
        .def_readwrite("sequence", &HybridPrefillRequest::sequence)
        .def_readwrite("request_id", &HybridPrefillRequest::request_id)
        .def_readwrite("hash_ids", &HybridPrefillRequest::hash_ids)
        .def_readwrite("input_tokens", &HybridPrefillRequest::input_tokens)
        .def_readwrite("max_prefix_tokens", &HybridPrefillRequest::max_prefix_tokens)
        .def_readwrite("affinity_id", &HybridPrefillRequest::affinity_id);

    py::class_<HybridPrefillResult>(module, "HybridPrefillResult")
        .def_readonly("sequence", &HybridPrefillResult::sequence)
        .def_readonly("request_id", &HybridPrefillResult::request_id)
        .def_readonly("input_tokens", &HybridPrefillResult::input_tokens)
        .def_readonly("matched_prefix_tokens", &HybridPrefillResult::matched_prefix_tokens)
        .def_readonly("full_available_prefix_tokens", &HybridPrefillResult::full_available_prefix_tokens)
        .def_readonly("restore_tokens", &HybridPrefillResult::restore_tokens)
        .def_readonly("restore_hash_id", &HybridPrefillResult::restore_hash_id)
        .def_readonly("full", &HybridPrefillResult::full)
        .def_readonly("linear", &HybridPrefillResult::linear)
        .def_readonly("prefill_compute_tokens", &HybridPrefillResult::prefill_compute_tokens)
        .def_readonly("replayed_prefix_tokens", &HybridPrefillResult::replayed_prefix_tokens)
        .def_readonly("new_suffix_tokens", &HybridPrefillResult::new_suffix_tokens)
        .def_readonly("generated_checkpoint_tokens", &HybridPrefillResult::generated_checkpoint_tokens);

    py::class_<HybridEvent>(module, "HybridEvent")
        .def(py::init<>())
        .def_readwrite("timestamp_ns", &HybridEvent::timestamp_ns)
        .def_readwrite("sequence", &HybridEvent::sequence)
        .def_readwrite("action", &HybridEvent::action)
        .def_readwrite("hash_ids", &HybridEvent::hash_ids)
        .def_readwrite("request_id", &HybridEvent::request_id)
        .def_readwrite("components", &HybridEvent::components)
        .def_readwrite("restore_blocks", &HybridEvent::restore_blocks)
        .def_readwrite("size_bytes", &HybridEvent::size_bytes);

    py::class_<HybridResult>(module, "HybridResult")
        .def_readonly("sequence", &HybridResult::sequence)
        .def_readonly("request_id", &HybridResult::request_id)
        .def_readonly("restore_blocks", &HybridResult::restore_blocks)
        .def_readonly("full", &HybridResult::full)
        .def_readonly("linear", &HybridResult::linear)
        .def_readonly("all_available", &HybridResult::all_available)
        .def_readonly("all_dram", &HybridResult::all_dram);

    py::class_<SimulationConfig>(module, "SimulationConfig")
        .def(py::init<>())
        .def_readwrite("block_size_bytes", &SimulationConfig::block_size_bytes)
        .def_readwrite("hybrid_attention", &SimulationConfig::hybrid_attention)
        .def_readwrite("memory", &SimulationConfig::memory)
        .def_readwrite("profile_memory", &SimulationConfig::profile_memory)
        .def_readwrite("slc", &SimulationConfig::slc)
        .def_readwrite("tlc", &SimulationConfig::tlc)
        .def_readwrite("simulation_end_ns", &SimulationConfig::simulation_end_ns)
        .def_readwrite(
            "progress_interval_requests",
            &SimulationConfig::progress_interval_requests
        );

    py::class_<Simulator>(module, "Simulator")
        .def(
            py::init([](
                         SimulationConfig config,
                         const std::string& trace_path,
                         const std::string& memory_policy,
                         bool admit_storage_hits,
                         const std::string& storage_policy,
                         const std::string& fixed_tier,
                         std::uint32_t fixed_stream_id,
                         double slc_write_ratio,
                         double slc_host_share,
                         double idle_multiplier,
                         double promotion_seconds,
                         double adaptation_gain,
                         double direct_gain,
                         double slc_soft_utilization,
                         double occupancy_decay,
                         double logical_fill_fraction,
                         double slc_erase_budget,
                         double tlc_erase_budget,
                         TimestampNs background_period_ns,
                         std::size_t memory_groups,
                         std::size_t memory_sampled_groups,
                         std::size_t memory_workers,
                         std::uint64_t memory_seed,
                         std::optional<TimestampNs> memory_retention_ns,
                         const std::string& rr_victim_search,
                         bool rr_subtree_counts,
                         bool rr_verify_victims,
                         bool rr_profile,
                         double memory_alpha,
                         std::optional<std::uint64_t> memory_max_eviction_blocks,
                         double memory_retention_growth_seconds_per_block,
                         std::optional<TimestampNs> memory_eviction_gap_reference_ns,
                         std::uint64_t memory_eviction_base_blocks,
                         bool online_tuning,
                         TimestampNs feedback_period_ns,
                         std::uint64_t shadow_page_bytes,
                         std::uint64_t shadow_pages_per_block,
                         double shadow_overprovisioning,
                         std::uint64_t shadow_slc_nominal_bytes,
                         std::uint64_t shadow_tlc_nominal_bytes,
                         double reuse_loss_budget,
                         std::uint64_t min_reuse_blocks,
                         std::uint64_t reuse_ema_scale_blocks,
                         std::uint64_t shadow_slc_physical_blocks,
                         std::uint64_t shadow_tlc_physical_blocks,
                         std::vector<double> memory_reuse_edges_s,
                         std::vector<std::vector<double>> memory_reuse_survival,
                         std::optional<double> memory_reuse_dump_probability,
                         bool memory_reuse_priority,
                         std::optional<double> memory_reuse_admit_probability,
                         double memory_reuse_short_horizon_s,
                         double memory_reuse_long_horizon_s,
                         std::vector<std::vector<std::vector<double>>> memory_reuse_prefix_keep,
                         bool memory_reuse_cost_weighted,
                         double memory_reuse_recompute_weight
                     ) {
                if (slc_host_share <= 0.0 || slc_host_share >= 1.0) {
                    throw py::value_error("slc_host_share must be between 0 and 1");
                }
                if (logical_fill_fraction <= 0.0 || logical_fill_fraction > 1.0) {
                    throw py::value_error("logical_fill_fraction must be in (0, 1]");
                }
                if (slc_erase_budget <= 0.0 || tlc_erase_budget <= 0.0) {
                    throw py::value_error("erase budgets must be positive");
                }
                if (memory_policy != "context_lru" &&
                    (memory_retention_growth_seconds_per_block != 0 || memory_eviction_gap_reference_ns)) {
                    throw py::value_error("adaptive memory parameters require context_lru");
                }
                if (memory_max_eviction_blocks && memory_policy != "context_lru" && memory_policy != "reuse_lru") {
                    throw py::value_error("max_eviction_blocks requires context_lru or reuse_lru");
                }
                std::unique_ptr<MemoryPolicy> memory;
                if (memory_policy == "baseline_lru") {
                    if (memory_groups != 1 || memory_sampled_groups != 0 || memory_workers != 1 ||
                        memory_retention_ns.has_value()) {
                        throw py::value_error("baseline_lru does not use groups, sampling, workers or retention");
                    }
                    memory = std::make_unique<BaselineMemoryLruPolicy>(admit_storage_hits);
                } else if (memory_policy == "indexed_lru") {
                    memory = std::make_unique<IndexedMemoryLruPolicy>(
                        admit_storage_hits, memory_groups, memory_sampled_groups,
                        memory_workers, memory_seed, memory_retention_ns);
                } else if (memory_policy == "reuse_lru") {
                    if (memory_groups != 1 || memory_sampled_groups != 0 || memory_workers != 1 ||
                        config.block_size_bytes == 0) {
                        throw py::value_error("reuse_lru requires positive block size and single exact group");
                    }
                    memory = std::make_unique<ReuseMemoryLruPolicy>(admit_storage_hits,
                        config.memory.capacity_bytes / config.block_size_bytes, memory_alpha,
                        memory_retention_ns, memory_max_eviction_blocks, memory_reuse_edges_s,
                        memory_reuse_survival, memory_reuse_dump_probability, memory_reuse_priority,
                        memory_reuse_admit_probability, memory_reuse_short_horizon_s,
                        memory_reuse_long_horizon_s, memory_reuse_prefix_keep,
                        memory_reuse_cost_weighted, memory_reuse_recompute_weight);
                } else if (memory_policy == "context_lru") {
                    if (memory_groups != 1 || memory_sampled_groups != 0 || memory_workers != 1) {
                        throw py::value_error("context_lru requires groups=1, sampled_groups=0, workers=1");
                    }
                    if (config.block_size_bytes == 0) {
                        throw py::value_error("block_size_bytes must be positive");
                    }
                    memory = std::make_unique<ContextMemoryLruPolicy>(
                        admit_storage_hits, config.memory.capacity_bytes / config.block_size_bytes,
                        memory_alpha, memory_retention_ns, memory_max_eviction_blocks,
                        memory_retention_growth_seconds_per_block, memory_eviction_gap_reference_ns,
                        memory_eviction_base_blocks);
                } else {
                    throw py::value_error("memory policy must be 'baseline_lru', 'indexed_lru', 'context_lru' or 'reuse_lru'");
                }
                const WearShareRoundRobinPolicyConfig round_robin{
                    slc_host_share,
                    logical_fill_fraction,
                    rr_victim_search,
                    rr_subtree_counts,
                    rr_verify_victims,
                    rr_profile,
                };
                const WearShareAffinityPolicyConfig affinity{
                    slc_host_share,
                    logical_fill_fraction,
                };
                const AdaptiveEndurancePolicyConfig adaptive{
                    idle_multiplier,
                    promotion_seconds,
                    adaptation_gain,
                    direct_gain,
                    slc_soft_utilization,
                    occupancy_decay,
                    logical_fill_fraction,
                    slc_erase_budget,
                    tlc_erase_budget,
                    background_period_ns,
                };
                const WearBalancedPolicyConfig wear_balanced{
                    idle_multiplier, promotion_seconds, adaptation_gain, direct_gain,
                    slc_soft_utilization, occupancy_decay, logical_fill_fraction,
                    slc_erase_budget, tlc_erase_budget,
                    ShadowConfig{online_tuning, feedback_period_ns, shadow_page_bytes,
                        shadow_pages_per_block, shadow_overprovisioning, shadow_slc_nominal_bytes,
                        shadow_tlc_nominal_bytes, slc_erase_budget, tlc_erase_budget,
                        reuse_loss_budget, min_reuse_blocks, reuse_ema_scale_blocks,
                        shadow_slc_physical_blocks, shadow_tlc_physical_blocks},
                    background_period_ns,
                };
                config.infinite_storage = storage_policy == "infinite_storage";
                auto storage = make_storage_policy(
                    storage_policy,
                    config,
                    fixed_tier,
                    fixed_stream_id,
                    slc_write_ratio,
                    round_robin,
                    affinity,
                    adaptive,
                    wear_balanced
                );
                return std::make_unique<Simulator>(
                    std::move(config),
                    std::move(memory),
                    std::move(storage),
                    std::filesystem::path(trace_path)
                );
            }),
            py::arg("config"),
            py::arg("trace_path"),
            py::arg("memory_policy"),
            py::arg("admit_storage_hits"),
            py::arg("storage_policy"),
            py::arg("fixed_tier"),
            py::arg("fixed_stream_id"),
            py::arg("slc_write_ratio"),
            py::arg("slc_host_share"),
            py::arg("idle_multiplier"),
            py::arg("promotion_seconds"),
            py::arg("adaptation_gain"),
            py::arg("direct_gain"),
            py::arg("slc_soft_utilization"),
            py::arg("occupancy_decay"),
            py::arg("logical_fill_fraction"),
            py::arg("slc_erase_budget"),
            py::arg("tlc_erase_budget"),
            py::arg("background_period_ns"),
            py::arg("memory_groups") = 1,
            py::arg("memory_sampled_groups") = 0,
            py::arg("memory_workers") = 1,
            py::arg("memory_seed") = 0,
            py::arg("memory_retention_ns") = py::none(),
            py::arg("rr_victim_search") = "indexed",
            py::arg("rr_subtree_counts") = false,
            py::arg("rr_verify_victims") = false,
            py::arg("rr_profile") = false,
            py::arg("memory_alpha") = 0.01,
            py::arg("memory_max_eviction_blocks") = py::none(),
            py::arg("memory_retention_growth_seconds_per_block") = 0.0,
            py::arg("memory_eviction_gap_reference_ns") = py::none(),
            py::arg("memory_eviction_base_blocks") = 64,
            py::arg("online_tuning") = false,
            py::arg("feedback_period_ns") = 900000000000ULL,
            py::arg("shadow_page_bytes") = 4096ULL,
            py::arg("shadow_pages_per_block") = 256ULL,
            py::arg("shadow_overprovisioning") = 0.07,
            py::arg("shadow_slc_nominal_bytes") = 0ULL,
            py::arg("shadow_tlc_nominal_bytes") = 0ULL,
            py::arg("reuse_loss_budget") = 0.01,
            py::arg("min_reuse_blocks") = 4096ULL,
            py::arg("reuse_ema_scale_blocks") = 50000ULL,
            py::arg("shadow_slc_physical_blocks") = 0ULL,
            py::arg("shadow_tlc_physical_blocks") = 0ULL,
            py::arg("memory_reuse_edges_s") = std::vector<double>{},
            py::arg("memory_reuse_survival") = std::vector<std::vector<double>>{},
            py::arg("memory_reuse_dump_probability") = py::none(),
            py::arg("memory_reuse_priority") = false,
            py::arg("memory_reuse_admit_probability") = py::none(),
            py::arg("memory_reuse_short_horizon_s") = 60.0,
            py::arg("memory_reuse_long_horizon_s") = 3600.0,
            py::arg("memory_reuse_prefix_keep") = std::vector<std::vector<std::vector<double>>>{},
            py::arg("memory_reuse_cost_weighted") = false,
            py::arg("memory_reuse_recompute_weight") = 1.0
        )
        .def("storage_performance", [](const Simulator& simulator) {
            py::dict result;
            const auto work = simulator.storage_policy_work();
            result["decisions"] = work.decisions;
            result["decision_ns"] = work.decision_ns;
            result["maintenance_ns"] = work.maintenance_ns;
            result["entries_examined"] = work.entries_examined;
            result["candidates_examined"] = work.candidates_examined;
            result["segment_nodes_examined"] = work.segment_nodes_examined;
            result["ancestor_updates"] = work.ancestor_updates;
            result["verified_decisions"] = work.verified_decisions;
            result["indexed_segments"] = work.indexed_segments;
            result["ancestor_entries"] = work.ancestor_entries;
            return result;
        })
        .def("enable_memory_diagnostics", &Simulator::enable_memory_diagnostics)
        .def("enable_request_diagnostics", &Simulator::enable_request_diagnostics)
        .def("memory_performance", [](const Simulator& simulator) {
            py::dict result;
            const auto work = simulator.memory_policy_work();
            result["decision_ns"] = simulator.memory_decision_ns;
            result["maintenance_ns"] = simulator.memory_maintenance_ns;
            result["decisions"] = simulator.memory_decisions;
            result["candidates_examined"] = work.candidates_examined;
            result["topology_nodes_examined"] = work.topology_nodes_examined;
            result["topology_member_moves"] = work.topology_member_moves;
            result["ancestor_updates"] = work.ancestor_updates;
            result["worker_rounds"] = work.worker_rounds;
            result["evicted_idle_bins"] = work.evicted_idle_bins;
            result["evicted_residency_bins"] = work.evicted_residency_bins;
            result["indexed_segments"] = work.indexed_segments;
            result["indexed_residents"] = work.indexed_residents;
            result["ancestor_entries"] = work.ancestor_entries;
            return result;
        })
        .def(
            "process",
            py::overload_cast<
                TimestampNs,
                RequestId,
                AffinityId,
                const std::vector<HashId>&
            >(&Simulator::process_request),
            py::call_guard<py::gil_scoped_release>()
        )
        .def(
            "process_batch",
            [](Simulator& simulator,
               const py::buffer& timestamps,
               const py::buffer& request_ids,
               const py::buffer& affinity_ids,
               const py::buffer& offsets,
               const py::buffer& hash_ids) {
                const py::buffer_info timestamp_info = require_u64_buffer(
                    timestamps,
                    "timestamps"
                );
                const py::buffer_info request_info = require_u64_buffer(
                    request_ids,
                    "request_ids"
                );
                const py::buffer_info affinity_info = require_u64_buffer(
                    affinity_ids,
                    "affinity_ids"
                );
                const py::buffer_info offset_info = require_u64_buffer(offsets, "offsets");
                const py::buffer_info hash_info = require_u64_buffer(hash_ids, "hash_ids");

                const auto request_count = static_cast<std::size_t>(timestamp_info.shape[0]);
                if (
                    request_info.shape[0] != timestamp_info.shape[0] ||
                    affinity_info.shape[0] != timestamp_info.shape[0]
                ) {
                    throw py::value_error(
                        "timestamps, request_ids, and affinity_ids must have equal length"
                    );
                }
                if (offset_info.shape[0] != static_cast<py::ssize_t>(request_count + 1)) {
                    throw py::value_error("offsets length must equal request_count + 1");
                }

                const auto* timestamp_data = static_cast<const TimestampNs*>(
                    timestamp_info.ptr
                );
                const auto* request_data = static_cast<const RequestId*>(request_info.ptr);
                const auto* affinity_data = static_cast<const AffinityId*>(affinity_info.ptr);
                const auto* offset_data = static_cast<const std::uint64_t*>(offset_info.ptr);
                const auto* hash_data = static_cast<const HashId*>(hash_info.ptr);
                const auto hash_count = static_cast<std::uint64_t>(hash_info.shape[0]);

                if (offset_data[0] != 0 || offset_data[request_count] != hash_count) {
                    throw py::value_error("offsets must span the complete hash_ids buffer");
                }
                for (std::size_t index = 0; index < request_count; ++index) {
                    if (offset_data[index] > offset_data[index + 1]) {
                        throw py::value_error("offsets must be nondecreasing");
                    }
                }

                py::gil_scoped_release release;
                for (std::size_t index = 0; index < request_count; ++index) {
                    const std::uint64_t begin = offset_data[index];
                    const std::uint64_t end = offset_data[index + 1];
                    simulator.process_request(
                        timestamp_data[index],
                        request_data[index],
                        affinity_data[index],
                        hash_data + begin,
                        static_cast<std::size_t>(end - begin)
                    );
                }
            }
        )
        .def("process_hybrid_event", &Simulator::process_hybrid_event,
            py::call_guard<py::gil_scoped_release>())
        .def("process_hybrid_events", [](Simulator& simulator, const std::vector<HybridEvent>& events) {
            simulator.hybrid_stats();
            std::vector<HybridResult> results;
            py::gil_scoped_release release;
            for (const auto& event : events) {
                auto result = simulator.process_hybrid_event(event);
                if (result) { results.push_back(std::move(*result)); }
            }
            return results;
        })
        .def("process_hybrid_batch", [](Simulator& simulator, const py::buffer& timestamps,
            const py::buffer& sequences, const py::buffer& actions, const py::buffer& components,
            const py::buffer& request_ids, const py::buffer& restore_blocks, const py::buffer& sizes,
            const py::buffer& offsets, const py::buffer& hashes) {
            simulator.hybrid_stats();
            // One validation/dispatch per columnar batch; no per-block Python callback.
            const auto t = require_u64_buffer(timestamps, "timestamps");
            const auto seq = require_u64_buffer(sequences, "sequences");
            const auto act = require_u64_buffer(actions, "actions");
            const auto comp = require_u64_buffer(components, "components");
            const auto req = require_u64_buffer(request_ids, "request_ids");
            const auto boundary = require_u64_buffer(restore_blocks, "restore_blocks");
            const auto size = require_u64_buffer(sizes, "sizes");
            const auto off = require_u64_buffer(offsets, "offsets");
            const auto hash = require_u64_buffer(hashes, "hashes");
            const auto n = t.shape[0];
            for (const auto* column : {&seq, &act, &comp, &req, &boundary, &size}) {
                if (column->shape[0] != n) { throw py::value_error("hybrid column lengths must match"); }
            }
            if (off.shape[0] != n + 1) { throw py::value_error("offsets length must be event_count + 1"); }
            const auto* o = static_cast<const std::uint64_t*>(off.ptr);
            const auto* a = static_cast<const std::uint64_t*>(act.ptr);
            const auto* c = static_cast<const std::uint64_t*>(comp.ptr);
            if (o[0] != 0 || o[n] != static_cast<std::uint64_t>(hash.shape[0])) {
                throw py::value_error("offsets must span the hashes buffer");
            }
            for (py::ssize_t i = 0; i < n; ++i) {
                if (o[i] >= o[i + 1] || a[i] > 2 || c[i] > 2) {
                    throw py::value_error("invalid offsets or hybrid event code");
                }
            }
            const auto* h = static_cast<const std::uint64_t*>(hash.ptr);
            std::vector<HybridResult> results;
            py::gil_scoped_release release;
            for (py::ssize_t i = 0; i < n; ++i) {
                HybridEvent e;
                e.timestamp_ns = static_cast<const std::uint64_t*>(t.ptr)[i];
                e.sequence = static_cast<const std::uint64_t*>(seq.ptr)[i];
                e.action = a[i] == 0 ? "observe" : a[i] == 1 ? "publish" : "request";
                e.components = c[i] == 0 ? "full" : c[i] == 1 ? "linear" : "both";
                e.request_id = static_cast<const std::uint64_t*>(req.ptr)[i];
                e.restore_blocks = static_cast<const std::uint64_t*>(boundary.ptr)[i];
                e.size_bytes = static_cast<const std::uint64_t*>(size.ptr)[i];
                e.hash_ids.assign(h + o[i], h + o[i + 1]);
                auto result = simulator.process_hybrid_event(e);
                if (result) { results.push_back(std::move(*result)); }
            }
            return results;
        })
        .def("process_hybrid_prefill", &Simulator::process_hybrid_prefill,
            py::arg("request"), py::arg("collect_results") = true,
            py::call_guard<py::gil_scoped_release>())
        .def("run_hybrid_prefill_requests", [](Simulator& simulator,
            const std::vector<HybridPrefillRequest>& requests) {
            py::gil_scoped_release release;
            for (const auto& request : requests) { simulator.process_hybrid_prefill(request, false); }
        })
        .def("process_hybrid_prefill_requests", [](Simulator& simulator,
            const std::vector<HybridPrefillRequest>& requests) {
            simulator.hybrid_stats();
            std::vector<HybridPrefillResult> results;
            py::gil_scoped_release release;
            for (const auto& request : requests) { results.push_back(simulator.process_hybrid_prefill(request)); }
            return results;
        })
        .def("process_hybrid_prefill_batch", [](Simulator& simulator, const py::buffer& timestamps,
            const py::buffer& sequences, const py::buffer& request_ids, const py::buffer& input_tokens,
            const py::buffer& max_prefix_tokens, const py::buffer& offsets, const py::buffer& hashes) {
            simulator.hybrid_stats();
            const auto t = require_u64_buffer(timestamps, "timestamps");
            const auto seq = require_u64_buffer(sequences, "sequences");
            const auto req = require_u64_buffer(request_ids, "request_ids");
            const auto tokens = require_u64_buffer(input_tokens, "input_tokens");
            const auto limits = require_u64_buffer(max_prefix_tokens, "max_prefix_tokens");
            const auto off = require_u64_buffer(offsets, "offsets");
            const auto hash = require_u64_buffer(hashes, "hashes");
            const auto n = t.shape[0];
            for (const auto* column : {&seq, &req, &tokens, &limits}) {
                if (column->shape[0] != n) { throw py::value_error("prefill column lengths must match"); }
            }
            if (off.shape[0] != n + 1) { throw py::value_error("offsets length must be request_count + 1"); }
            const auto* o = static_cast<const std::uint64_t*>(off.ptr);
            if (o[0] != 0 || o[n] != static_cast<std::uint64_t>(hash.shape[0])) {
                throw py::value_error("offsets must span the hashes buffer");
            }
            for (py::ssize_t i = 0; i < n; ++i) {
                if (o[i] > o[i + 1]) { throw py::value_error("offsets must be nondecreasing"); }
            }
            const auto* h = static_cast<const std::uint64_t*>(hash.ptr);
            std::vector<HybridPrefillResult> results;
            py::gil_scoped_release release;
            for (py::ssize_t i = 0; i < n; ++i) {
                HybridPrefillRequest q;
                q.timestamp_ns = static_cast<const std::uint64_t*>(t.ptr)[i];
                q.sequence = static_cast<const std::uint64_t*>(seq.ptr)[i];
                q.request_id = static_cast<const std::uint64_t*>(req.ptr)[i];
                q.input_tokens = static_cast<const std::uint64_t*>(tokens.ptr)[i];
                const auto limit = static_cast<const std::uint64_t*>(limits.ptr)[i];
                if (limit != std::numeric_limits<std::uint64_t>::max()) { q.max_prefix_tokens = limit; }
                if (o[i] != o[i + 1]) { q.hash_ids.assign(h + o[i], h + o[i + 1]); }
                results.push_back(simulator.process_hybrid_prefill(q));
            }
            return results;
        })
        .def("hybrid_inspect", [](const Simulator& simulator, NodeId id, const std::string& kind) {
            if (kind != "full" && kind != "linear") { throw py::value_error("component must be full or linear"); }
            const auto* c = simulator.hybrid_component(id, kind == "full" ? 0 : 1);
            py::dict result;
            result["published"] = c && c->published;
            result["in_dram"] = c && c->in_dram;
            result["on_storage"] = c && c->on_storage();
            result["storage_allocated_bytes"] = c ? c->storage_units * simulator.config().block_size_bytes : 0;
            result["size_bytes"] = c ? c->size_bytes : 0;
            result["demands"] = c ? c->demands : 0;
            result["last_demand_ns"] = c ? c->last_demand_ns : 0;
            if (c && simulator.hybrid_stats().cost) {
                const auto& cost = *simulator.hybrid_stats().cost;
                const auto component = kind == "full" ? 0U : 1U;
                const auto& h = cost.history(id, component);
                result["logical_demands"] = h.count;
                result["logical_last_ns"] = h.last_ns;
            }
            result["storage_addresses"] = c ? c->storage_addresses : std::vector<std::uint64_t>{};
            if (c && c->on_storage() && !simulator.config().infinite_storage) {
                const auto& node = simulator.tree().node(id);
                result["storage_tier"] = node.storage_tier == StorageTier::Slc ? "slc" : "tlc";
                result["stream_id"] = node.storage_stream_id;
            }
            return result;
        })
        .def("hybrid_stats", [](const Simulator& simulator) {
            const auto& state = simulator.hybrid_stats();
            py::dict result;
            const bool prefill = simulator.config().hybrid_attention.replay_mode == "prefix_replay";
            result["schema_version"] = prefill ? 2 : 1;
            result["io_events"] = state.io_sequence;
            result["requests"] = state.requests;
            if (prefill) {
                const auto& c = state.prefill;
                py::dict row;
                row["restored_requests"] = c.restored_requests;
                row["dram_restored_requests"] = c.dram_restored_requests;
                row["input_tokens"] = c.input_tokens;
                row["matched_prefix_tokens"] = c.matched_prefix_tokens;
                row["restored_tokens"] = c.restored_tokens;
                row["prefill_compute_tokens"] = c.prefill_compute_tokens;
                row["replayed_prefix_tokens"] = c.replayed_prefix_tokens;
                row["new_suffix_tokens"] = c.new_suffix_tokens;
                row["generated_checkpoints"] = c.generated_checkpoints;
                row["joint_token_hit_rate"] = rate(c.restored_tokens, c.input_tokens);
                row["joint_restored_request_rate"] = rate(c.restored_requests, state.requests);
                result["prefill"] = row;
            } else {
                result["all_available_requests"] = state.all_available_requests;
                result["all_dram_requests"] = state.all_dram_requests;
                result["joint_requests"] = state.joint_requests;
                result["joint_available"] = state.joint_available;
                result["joint_dram"] = state.joint_dram;
            }
            result["tree_nodes"] = simulator.tree().size();
            if (!simulator.config().infinite_storage) {
                py::dict storage;
                storage["schema_version"] = 2;
                storage["placement_unit"] = "prefix-node bundle; independent FULL/LINEAR payloads";
                storage["background_ticks"] = simulator.metrics().background_ticks;
                const auto policy = simulator.storage_policy_stats();
                storage["gap_samples"] = policy.gap_samples;
                storage["gap_q95_seconds"] = policy.gap_q95_seconds;
                storage["idle_threshold_seconds"] = policy.idle_threshold_seconds;
                for (std::size_t k = 0; k < 2; ++k) {
                    const auto tier = k == 0 ? StorageTier::Slc : StorageTier::Tlc;
                    const auto& c = state.storage_counters[k];
                    const auto& pool = simulator.storage().tier(tier);
                    py::dict row;
                    row["capacity_bytes"] = pool.capacity_blocks() * simulator.config().block_size_bytes;
                    row["used_bytes"] = pool.used_blocks() * simulator.config().block_size_bytes;
                    row["peak_used_bytes"] = pool.peak_used_blocks() * simulator.config().block_size_bytes;
                    row["reads"] = c.reads; row["writes"] = c.writes; row["trims"] = c.trims;
                    row["read_bytes"] = c.read_bytes; row["write_bytes"] = c.write_bytes;
                    row["trim_bytes"] = c.trim_bytes;
                    row["capacity_trim_segments"] = c.capacity_trims;
                    row["idle_trim_segments"] = c.idle_trims;
                    row["migrated_objects"] = c.migrated_objects;
                    storage[k == 0 ? "slc" : "tlc"] = row;
                }
                result["storage"] = storage;
            }
            const auto& cfg = simulator.config().hybrid_attention;
            if (state.cost) {
                const auto& policy = *state.cost;
                const auto& c = policy.counters();
                py::dict row;
                row["decisions"] = c.decisions; row["quotes"] = c.quotes;
                row["infeasible_routes"] = c.infeasible;
                row["scenario_evaluations"] = c.scenario_evaluations;
                row["logical_full_demands"] = c.logical_full;
                row["logical_linear_demands"] = c.logical_linear;
                row["drop_cost"] = c.drop; row["saved_cost"] = c.benefit;
                row["replacement_cost"] = c.replacement; row["media_cost"] = c.media;
                row["queue_cost"] = c.queue; row["total_cost"] = c.total;
                row["predicted_nand_bytes"] = c.predicted_nand_bytes;
                row["selected_route_writes"] = c.selected_routes;
                row["hazard_fit_squared_error"] = c.hazard_residual;
                row["history_entries"] = policy.history_entries();
                row["shared_model_bytes"] = policy.reuse().shared_bytes();
                row["pending_forecasts"] = policy.reuse().pending_forecasts();
                row["pending_payload_bytes"] = policy.reuse().pending_payload_bytes();
                py::dict predictions;
                for (std::size_t kind = 0; kind < 2; ++kind) {
                    py::list samples;
                    for (std::size_t h = 0; h < 3; ++h) {
                        const auto& score = policy.reuse().scores(kind, h);
                        py::dict sample;
                        sample["horizon_seconds"] = cfg.cost.horizons_seconds[h];
                        sample["samples"] = score.samples; sample["positives"] = score.positives;
                        sample["censored"] = score.censored;
                        sample["brier_sum"] = score.brier_sum;
                        samples.append(sample);
                    }
                    predictions[kind == 0 ? "full" : "linear"] = samples;
                }
                row["reuse"] = predictions;
                result["cost"] = row;
            }
            if (cfg.pool_allocation != "fixed") {
                py::dict row;
                row["allocation"] = cfg.pool_allocation;
                row["capacity_bytes"] = simulator.config().memory.capacity_bytes;
                row["resident_bytes"] = state.counters[0].resident_bytes + state.counters[1].resident_bytes;
                row["peak_resident_bytes"] = state.peak_resident_bytes;
                if (cfg.pool_allocation == "adaptive") {
                    row["full_target_bytes"] = state.target_bytes[0];
                    row["linear_target_bytes"] = state.target_bytes[1];
                    row["updates"] = state.pool_updates;
                    row["cross_pool_reclaims"] = state.cross_pool_reclaims;
                } else { row["cross_component_reclaims"] = state.cross_pool_reclaims; }
                result["pool"] = row;
            }
            for (std::size_t k = 0; k < 2; ++k) {
                const auto& c = state.counters[k];
                py::dict row;
                row["demands"] = c.demands;
                row["dram_hits"] = c.dram_hits;
                row["storage_hits"] = c.storage_hits;
                row["never_published_misses"] = c.never_published_misses;
                row["lost_misses"] = c.lost_misses;
                row["publications"] = c.publications;
                row["published_bytes"] = c.published_bytes;
                row["resident_bytes"] = c.resident_bytes;
                row["peak_resident_bytes"] = c.peak_resident_bytes;
                row["resident_objects"] = c.resident_objects;
                row["storage_bytes"] = c.storage_bytes;
                row["storage_allocated_bytes"] = c.storage_allocated_bytes;
                row["read_objects"] = c.read_objects;
                row["read_bytes"] = c.read_bytes;
                row["read_allocated_bytes"] = c.read_allocated_bytes;
                row["write_objects"] = c.write_objects;
                row["write_bytes"] = c.write_bytes;
                row["write_allocated_bytes"] = c.write_allocated_bytes;
                row["evictions"] = c.evictions;
                row["bypasses"] = c.bypasses;
                if (!simulator.config().infinite_storage) {
                    row["trim_objects"] = c.trim_objects;
                    row["trim_bytes"] = c.trim_bytes;
                    row["admission_rejections"] = c.admission_rejections;
                }
                row["candidates_examined"] = c.candidates_examined;
                if (prefill || cfg.eviction_mode == "retention" || simulator.config().hybrid_attention.full_drop_fraction ||
                    simulator.config().hybrid_attention.linear_drop_fraction) {
                    row["eligible_evictions"] = c.eligible_evictions;
                    row["drop_objects"] = c.drop_objects;
                    row["dump_objects"] = c.dump_objects;
                    row["backed_evictions"] = c.backed_evictions;
                }
                if (cfg.eviction_mode == "retention" || cfg.eviction_mode == "joint_cost") {
                    row["evicted_bytes"] = c.evicted_bytes;
                    row["eligible_bytes"] = c.eligible_bytes;
                    row["lost_bytes"] = c.lost_bytes;
                    row["dump_bytes"] = c.dump_bytes;
                    row["backed_bytes"] = c.backed_bytes;
                    row["drop_action_objects"] = c.drop_action_objects;
                    row["drop_action_bytes"] = c.drop_action_bytes;
                }
                result[k == 0 ? "full" : "linear"] = row;
            }
            return result;
        })
        .def("stats", &simulator_stats)
        .def(
            "finish",
            py::overload_cast<>(&Simulator::finish),
            py::call_guard<py::gil_scoped_release>()
        )
        .def_property_readonly("node_count", [](const Simulator& simulator) {
            return simulator.tree().size();
        })
        .def_property_readonly("trace_event_count", &Simulator::trace_event_count);
}
