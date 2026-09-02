#pragma once

#include <array>
#include <cstdint>
#include <vector>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

struct MediumSummary {
    std::uint64_t capacity_blocks;
    std::uint64_t used_blocks;
    std::uint32_t stream_count;
};

struct StorageSummary {
    MediumSummary slc;
    MediumSummary tlc;
};

class MemoryPolicy {
  public:
    virtual ~MemoryPolicy() = default;

    virtual bool admit_storage_hit(const AccessContext& context, const Node& node) = 0;
    virtual NodeId choose_victim(const AccessContext& context) = 0;
    virtual EvictionAction eviction_action(
        const Node& victim,
        const AccessContext& context
    ) = 0;

    virtual void on_memory_insert(NodeId node_id) = 0;
    virtual void on_memory_access(NodeId node_id) = 0;
    virtual void on_memory_remove(NodeId node_id) = 0;
};

class WritePlacementPolicy {
  public:
    virtual ~WritePlacementPolicy() = default;

    virtual Placement place(
        const Node& node,
        const AccessContext& context,
        const StorageSummary& storage
    ) = 0;
};

class StorageEvictionPolicy {
  public:
    virtual ~StorageEvictionPolicy() = default;

    virtual NodeId choose_victim(
        Medium medium,
        NodeId incoming_node,
        const AccessContext& context
    ) = 0;

    virtual void on_storage_read(NodeId node_id, Medium medium) = 0;
    virtual void on_storage_write(NodeId node_id, Medium medium) = 0;
    virtual void on_storage_remove(NodeId node_id, Medium medium) = 0;
};

class LruMemoryPolicy final : public MemoryPolicy {
  public:
    explicit LruMemoryPolicy(
        bool admit_storage_hits = true,
        EvictionAction eviction_action = EvictionAction::Persist
    );

    bool admit_storage_hit(const AccessContext& context, const Node& node) override;
    NodeId choose_victim(const AccessContext& context) override;
    EvictionAction eviction_action(
        const Node& victim,
        const AccessContext& context
    ) override;

    void on_memory_insert(NodeId node_id) override;
    void on_memory_access(NodeId node_id) override;
    void on_memory_remove(NodeId node_id) override;

  private:
    struct Link {
        NodeId previous = kInvalidNodeId;
        NodeId next = kInvalidNodeId;
    };

    void ensure_node(NodeId node_id);
    void attach_front(NodeId node_id) noexcept;
    void detach(NodeId node_id) noexcept;

    bool admit_storage_hits_;
    EvictionAction eviction_action_;
    std::vector<Link> links_;
    NodeId head_ = kInvalidNodeId;
    NodeId tail_ = kInvalidNodeId;
};

class FixedPlacementPolicy final : public WritePlacementPolicy {
  public:
    explicit FixedPlacementPolicy(Medium medium, std::uint32_t stream_id = 0);

    Placement place(
        const Node& node,
        const AccessContext& context,
        const StorageSummary& storage
    ) override;

  private:
    Placement placement_;
};

class RatioPlacementPolicy final : public WritePlacementPolicy {
  public:
    RatioPlacementPolicy(
        double slc_ratio,
        std::uint32_t slc_stream_count,
        std::uint32_t tlc_stream_count
    );

    Placement place(
        const Node& node,
        const AccessContext& context,
        const StorageSummary& storage
    ) override;

  private:
    double slc_ratio_;
    std::array<std::uint64_t, 2> write_counts_{};
    std::array<std::uint32_t, 2> next_stream_{};
    std::array<std::uint32_t, 2> stream_counts_;
};

class LruStorageEvictionPolicy final : public StorageEvictionPolicy {
  public:
    NodeId choose_victim(
        Medium medium,
        NodeId incoming_node,
        const AccessContext& context
    ) override;

    void on_storage_read(NodeId node_id, Medium medium) override;
    void on_storage_write(NodeId node_id, Medium medium) override;
    void on_storage_remove(NodeId node_id, Medium medium) override;

  private:
    struct Link {
        NodeId previous = kInvalidNodeId;
        NodeId next = kInvalidNodeId;
        Medium medium = Medium::Slc;
    };

    void ensure_node(NodeId node_id);
    void attach_front(NodeId node_id, Medium medium) noexcept;
    void detach(NodeId node_id) noexcept;

    std::vector<Link> links_;
    std::array<NodeId, 2> heads_{kInvalidNodeId, kInvalidNodeId};
    std::array<NodeId, 2> tails_{kInvalidNodeId, kInvalidNodeId};
};

}  // namespace dwpdsim
