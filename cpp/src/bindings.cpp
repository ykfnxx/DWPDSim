#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstddef>
#include <cstdint>
#include <filesystem>
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

MemoryEvictionAction parse_memory_action(const std::string& value) {
    if (value == "drop") {
        return MemoryEvictionAction::Drop;
    }
    if (value == "dump") {
        return MemoryEvictionAction::Dump;
    }
    throw py::value_error("memory eviction action must be 'drop' or 'dump'");
}

std::unique_ptr<StoragePolicy> make_storage_policy(
    const std::string& kind,
    const SimulationConfig& simulation,
    const std::string& fixed_tier,
    std::uint32_t fixed_stream_id,
    double slc_write_ratio,
    const WearShareRoundRobinPolicyConfig& round_robin,
    const WearShareAffinityPolicyConfig& affinity,
    const AdaptiveEndurancePolicyConfig& adaptive
) {
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
    if (kind == "adaptive_endurance") {
        return std::make_unique<AdaptiveEnduranceStoragePolicy>(adaptive);
    }
    throw py::value_error(
        "storage policy must be baseline_fixed_lru, baseline_ratio_lru, "
        "wear_share_round_robin, wear_share_affinity, or adaptive_endurance"
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

    py::dict accesses;
    accesses["requests"] = metrics.request_count;
    accesses["total"] = metrics.block_access_count;
    accesses["memory_hits"] = metrics.memory_hits;
    accesses["slc_hits"] = metrics.slc_hits;
    accesses["tlc_hits"] = metrics.tlc_hits;
    accesses["global_misses"] = metrics.global_misses;
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

    py::class_<SimulationConfig>(module, "SimulationConfig")
        .def(py::init<>())
        .def_readwrite("block_size_bytes", &SimulationConfig::block_size_bytes)
        .def_readwrite("memory", &SimulationConfig::memory)
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
                         const std::string& memory_eviction_action,
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
                         TimestampNs background_period_ns
                     ) {
                if (memory_policy != "baseline_lru") {
                    throw py::value_error("memory policy must be 'baseline_lru'");
                }
                if (slc_host_share <= 0.0 || slc_host_share >= 1.0) {
                    throw py::value_error("slc_host_share must be between 0 and 1");
                }
                if (logical_fill_fraction <= 0.0 || logical_fill_fraction > 1.0) {
                    throw py::value_error("logical_fill_fraction must be in (0, 1]");
                }
                if (slc_erase_budget <= 0.0 || tlc_erase_budget <= 0.0) {
                    throw py::value_error("erase budgets must be positive");
                }
                auto memory = std::make_unique<BaselineMemoryLruPolicy>(
                    admit_storage_hits,
                    parse_memory_action(memory_eviction_action)
                );
                const WearShareRoundRobinPolicyConfig round_robin{
                    slc_host_share,
                    logical_fill_fraction,
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
                auto storage = make_storage_policy(
                    storage_policy,
                    config,
                    fixed_tier,
                    fixed_stream_id,
                    slc_write_ratio,
                    round_robin,
                    affinity,
                    adaptive
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
            py::arg("memory_eviction_action"),
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
            py::arg("background_period_ns")
        )
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
