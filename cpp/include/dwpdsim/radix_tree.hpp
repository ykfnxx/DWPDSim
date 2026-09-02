#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <unordered_map>
#include <utility>
#include <vector>

#include "dwpdsim/types.hpp"

namespace dwpdsim {

struct EdgeKey {
    NodeId parent_id;
    HashId hash_id;

    bool operator==(const EdgeKey& other) const noexcept {
        return parent_id == other.parent_id && hash_id == other.hash_id;
    }
};

struct EdgeKeyHash {
    std::size_t operator()(const EdgeKey& key) const noexcept;
};

class RadixTree {
  public:
    RadixTree();

    std::pair<NodeId, bool> get_or_create(
        NodeId parent_id,
        HashId hash_id,
        Timestamp timestamp
    );

    std::optional<NodeId> find_child(NodeId parent_id, HashId hash_id) const;

    Node& node(NodeId node_id) noexcept;
    const Node& node(NodeId node_id) const noexcept;

    void record_access(NodeId node_id, Timestamp timestamp, bool hit) noexcept;

    std::size_t size() const noexcept;

  private:
    std::vector<Node> nodes_;
    std::unordered_map<EdgeKey, NodeId, EdgeKeyHash> child_index_;
};

}  // namespace dwpdsim
