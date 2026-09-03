#include "dwpdsim/policies/storage_policy.hpp"

#include <algorithm>

namespace dwpdsim {

StorageView::StorageView(
    const RadixTree& tree,
    const StorageState& storage,
    std::uint64_t block_size_bytes
)
    : tree_(tree), storage_(storage), block_size_bytes_(block_size_bytes) {}

SegmentView StorageView::resolve_segment(NodeId endpoint) const {
    SegmentView result;
    result.segment_endpoint = endpoint;
    result.segment_top = tree_.segment_top(endpoint);
    tree_.resolve_segment(endpoint, result.ordered_nodes);
    return result;
}

bool StorageView::is_storage_leaf(NodeId endpoint) const {
    const SegmentView segment = resolve_segment(endpoint);
    const bool resident = std::any_of(
        segment.ordered_nodes.begin(),
        segment.ordered_nodes.end(),
        [this](NodeId node_id) { return tree_.node(node_id).on_storage; }
    );
    return resident && !tree_.has_storage_descendant(endpoint);
}

std::uint64_t StorageView::resident_blocks(NodeId endpoint, StorageTier tier) const {
    const SegmentView segment = resolve_segment(endpoint);
    return static_cast<std::uint64_t>(std::count_if(
        segment.ordered_nodes.begin(),
        segment.ordered_nodes.end(),
        [this, tier](NodeId node_id) {
            const Node& node = tree_.node(node_id);
            return node.on_storage && node.storage_tier == tier;
        }
    ));
}

std::vector<NodeId> StorageView::resident_nodes(NodeId endpoint, StorageTier tier) const {
    SegmentView segment = resolve_segment(endpoint);
    std::vector<NodeId> result;
    result.reserve(segment.ordered_nodes.size());
    for (NodeId node_id : segment.ordered_nodes) {
        const Node& node = tree_.node(node_id);
        if (node.on_storage && node.storage_tier == tier) {
            result.push_back(node_id);
        }
    }
    return result;
}

bool StorageView::intersects_protected(
    NodeId endpoint,
    NodeSpan protected_nodes
) const {
    const SegmentView segment = resolve_segment(endpoint);
    for (NodeId node_id : segment.ordered_nodes) {
        if (std::find(protected_nodes.begin(), protected_nodes.end(), node_id) !=
            protected_nodes.end()) {
            return true;
        }
    }
    return false;
}

const RadixTree& StorageView::tree() const noexcept {
    return tree_;
}

const StorageState& StorageView::storage() const noexcept {
    return storage_;
}

std::uint64_t StorageView::block_size_bytes() const noexcept {
    return block_size_bytes_;
}

}  // namespace dwpdsim
