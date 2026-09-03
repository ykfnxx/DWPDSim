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
        info.ndim != 1 || info.itemsize != static_cast<py::ssize_t>(sizeof(std::uint64_t)) ||
        !unsigned_64_format ||
        info.strides[0] != static_cast<py::ssize_t>(sizeof(std::uint64_t))
    ) {
        throw py::value_error(std::string(name) + " must be a contiguous uint64 buffer");
    }
    return info;
}

Medium parse_medium(const std::string& value) {
    if (value == "slc") {
        return Medium::Slc;
    }
    if (value == "tlc") {
        return Medium::Tlc;
    }
    throw py::value_error("fixed_medium must be 'slc' or 'tlc'");
}

EvictionAction parse_eviction_action(const std::string& value) {
    if (value == "drop") {
        return EvictionAction::Drop;
    }
    if (value == "persist") {
        return EvictionAction::Persist;
    }
    throw py::value_error("memory eviction action must be 'drop' or 'persist'");
}

std::unique_ptr<WritePlacementPolicy> make_placement_policy(
    const std::string& policy,
    const SimulationConfig& config,
    const std::string& fixed_medium,
    std::uint32_t fixed_stream_id,
    double slc_write_ratio
) {
    if (policy == "fixed") {
        const Medium medium = parse_medium(fixed_medium);
        const std::uint32_t stream_count =
            medium == Medium::Slc ? config.slc.stream_count : config.tlc.stream_count;
        if (fixed_stream_id >= stream_count) {
            throw py::value_error("fixed stream_id is outside the configured medium");
        }
        return std::make_unique<FixedPlacementPolicy>(medium, fixed_stream_id);
    }
    if (policy == "ratio") {
        if (slc_write_ratio < 0.0 || slc_write_ratio > 1.0) {
            throw py::value_error("slc_write_ratio must be between 0 and 1");
        }
        return std::make_unique<RatioPlacementPolicy>(
            slc_write_ratio,
            config.slc.stream_count,
            config.tlc.stream_count
        );
    }
    throw py::value_error("placement policy must be 'fixed' or 'ratio'");
}

py::dict blocks_and_bytes(std::uint64_t blocks, std::uint64_t block_size_bytes) {
    py::dict result;
    result["blocks"] = blocks;
    result["bytes"] = blocks * block_size_bytes;
    return result;
}

double rate(std::uint64_t numerator, std::uint64_t denominator) {
    return denominator == 0 ? 0.0 : static_cast<double>(numerator) / denominator;
}

py::dict medium_metrics(
    const Simulator& simulator,
    Medium medium,
    const MediumConfig& config
) {
    const MetricsCollector& metrics = simulator.metrics();
    const std::size_t index = medium_index(medium);
    const MediumIoCounters& io = metrics.io[index];
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
    result["resident_blocks"] = metrics.storage_resident_blocks[index];
    result["peak_resident_blocks"] = metrics.peak_storage_resident_blocks[index];
    result["reads"] = blocks_and_bytes(io.reads, block_size);
    result["writes"] = blocks_and_bytes(io.writes, block_size);
    result["trims"] = blocks_and_bytes(io.trims, block_size);
    result["evicted_segments"] = metrics.storage_evicted_segments[index];
    result["evicted_blocks"] = metrics.storage_evicted_blocks[index];
    result["stream_writes"] = std::move(streams);
    return result;
}

py::dict simulator_stats(const Simulator& simulator) {
    const SimulationConfig& config = simulator.config();
    const MetricsCollector& metrics = simulator.metrics();
    const std::uint64_t storage_hits = metrics.slc_hits + metrics.tlc_hits;
    const std::uint64_t memory_misses = storage_hits + metrics.global_misses;
    const std::uint64_t all_hits = metrics.memory_hits + storage_hits;

    py::dict time;
    time["unit"] = config.timestamp_unit;
    if (metrics.start_timestamp.has_value()) {
        time["start_timestamp"] = *metrics.start_timestamp;
        time["end_timestamp"] = *metrics.end_timestamp;
        time["duration"] = *metrics.end_timestamp - *metrics.start_timestamp;
    } else {
        time["start_timestamp"] = py::none();
        time["end_timestamp"] = py::none();
        time["duration"] = 0;
    }

    py::dict configuration;
    configuration["block_size_bytes"] = config.block_size_bytes;
    configuration["memory_capacity_bytes"] = config.memory_capacity_bytes;
    configuration["slc_capacity_bytes"] = config.slc.capacity_bytes;
    configuration["tlc_capacity_bytes"] = config.tlc.capacity_bytes;

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
    memory["evictions"] = metrics.memory_evictions;
    memory["evicted_segments"] = metrics.memory_evicted_segments;
    memory["evicted_blocks"] = metrics.memory_evictions;
    memory["evictions_with_storage_copy"] = metrics.memory_evictions_with_storage_copy;
    memory["eviction_drops"] = metrics.memory_eviction_drops;
    memory["eviction_persists"] = metrics.memory_eviction_persists;

    py::dict storage;
    storage["slc"] = medium_metrics(simulator, Medium::Slc, config.slc);
    storage["tlc"] = medium_metrics(simulator, Medium::Tlc, config.tlc);
    storage["duplicated_blocks"] = metrics.duplicated_blocks;

    py::dict tree;
    tree["nodes"] = simulator.tree().size();
    tree["nodes_created"] = metrics.tree_nodes_created;
    tree["nodes_removed"] = metrics.tree_nodes_removed;

    py::dict trace;
    trace["schema_version"] = 1;
    trace["events"] = simulator.trace_event_count();

    py::dict result;
    result["time"] = std::move(time);
    result["configuration"] = std::move(configuration);
    result["accesses"] = std::move(accesses);
    result["created"] = blocks_and_bytes(metrics.global_misses, config.block_size_bytes);
    result["memory"] = std::move(memory);
    result["storage"] = std::move(storage);
    result["tree"] = std::move(tree);
    result["trace"] = std::move(trace);
    return result;
}

}  // namespace
}  // namespace dwpdsim

PYBIND11_MODULE(_core, module) {
    using namespace dwpdsim;

    module.doc() = "C++ core for DWPDSim";
    module.attr("CORE_VERSION") = "0.4.0";

    py::class_<MediumConfig>(module, "MediumConfig")
        .def(py::init<>())
        .def_readwrite("capacity_bytes", &MediumConfig::capacity_bytes)
        .def_readwrite("stream_count", &MediumConfig::stream_count);

    py::class_<SimulationConfig>(module, "SimulationConfig")
        .def(py::init<>())
        .def_readwrite("block_size_bytes", &SimulationConfig::block_size_bytes)
        .def_readwrite("memory_capacity_bytes", &SimulationConfig::memory_capacity_bytes)
        .def_readwrite("slc", &SimulationConfig::slc)
        .def_readwrite("tlc", &SimulationConfig::tlc)
        .def_readwrite("timestamp_unit", &SimulationConfig::timestamp_unit)
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
                         const std::string& placement_policy,
                         const std::string& fixed_medium,
                         std::uint32_t fixed_stream_id,
                         double slc_write_ratio,
                         const std::string& storage_eviction_policy
                     ) {
                if (memory_policy != "lru") {
                    throw py::value_error("memory policy must be 'lru'");
                }
                if (storage_eviction_policy != "lru") {
                    throw py::value_error("storage eviction policy must be 'lru'");
                }
                auto memory = std::make_unique<LruMemoryPolicy>(
                    admit_storage_hits,
                    parse_eviction_action(memory_eviction_action)
                );
                auto placement = make_placement_policy(
                    placement_policy,
                    config,
                    fixed_medium,
                    fixed_stream_id,
                    slc_write_ratio
                );
                return std::make_unique<Simulator>(
                    std::move(config),
                    std::move(memory),
                    std::move(placement),
                    std::make_unique<LruStorageEvictionPolicy>(),
                    std::filesystem::path(trace_path)
                );
            }),
            py::arg("config"),
            py::arg("trace_path"),
            py::arg("memory_policy") = "lru",
            py::arg("admit_storage_hits") = true,
            py::arg("memory_eviction_action") = "persist",
            py::arg("placement_policy") = "fixed",
            py::arg("fixed_medium") = "tlc",
            py::arg("fixed_stream_id") = 0,
            py::arg("slc_write_ratio") = 0.0,
            py::arg("storage_eviction_policy") = "lru"
        )
        .def(
            "process",
            py::overload_cast<Timestamp, const std::vector<HashId>&>(
                &Simulator::process_request
            ),
            py::call_guard<py::gil_scoped_release>()
        )
        .def(
            "process_batch",
            [](Simulator& simulator,
               const py::buffer& timestamps,
               const py::buffer& offsets,
               const py::buffer& hash_ids) {
                const py::buffer_info timestamp_info = require_u64_buffer(
                    timestamps,
                    "timestamps"
                );
                const py::buffer_info offset_info = require_u64_buffer(offsets, "offsets");
                const py::buffer_info hash_info = require_u64_buffer(hash_ids, "hash_ids");

                const auto request_count = static_cast<std::size_t>(timestamp_info.shape[0]);
                if (offset_info.shape[0] != static_cast<py::ssize_t>(request_count + 1)) {
                    throw py::value_error("offsets length must equal request_count + 1");
                }

                const auto* timestamp_data = static_cast<const Timestamp*>(timestamp_info.ptr);
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
                        hash_data + begin,
                        static_cast<std::size_t>(end - begin)
                    );
                }
            }
        )
        .def("stats", &simulator_stats)
        .def("finish", &Simulator::finish, py::call_guard<py::gil_scoped_release>())
        .def_property_readonly("node_count", [](const Simulator& simulator) {
            return simulator.tree().size();
        })
        .def_property_readonly("trace_event_count", &Simulator::trace_event_count);
}
