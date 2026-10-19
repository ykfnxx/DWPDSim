#include <cassert>
#include <cstdint>
#include <limits>
#include <type_traits>
#include <utility>
#include <vector>

#include "dwpdsim/radix_tree.hpp"

int main() {
    // Legacy aggregate initialization and copy/assignment are public contracts.
    dwpdsim::Node legacy{42, 10};
    const auto copied_node = legacy;
    assert(copied_node.hash_id == 42);
    assert(copied_node.first_seen_timestamp_ns == 10);
    assert(!copied_node.hybrid);
    legacy = copied_node;

    dwpdsim::RadixTree tree;

    const auto [zero, created_zero] = tree.get_or_create_root(0, 10);
    const auto [first, created_first] = tree.get_or_create(zero, 10, 10);
    const auto [second, created_second] = tree.get_or_create(first, 20, 10);
    const auto [leaf, created_leaf] = tree.get_or_create(second, 30, 10);
    const auto [branch_leaf, created_branch_leaf] = tree.get_or_create(first, 40, 20);
    const auto [same, reused] = tree.get_or_create(first, 20, 20);
    const dwpdsim::HashId maximum = std::numeric_limits<dwpdsim::HashId>::max();
    const auto [maximum_id, created_maximum] = tree.get_or_create_root(maximum, 20);

    assert(created_zero);
    assert(created_first);
    assert(created_second);
    assert(created_leaf);
    assert(created_branch_leaf);
    assert(!reused);
    assert(created_maximum);
    assert(zero == 0);
    assert(first == 10);
    assert(second == 20);
    assert(leaf == 30);
    assert(branch_leaf == 40);
    assert(same == second);
    assert(maximum_id == maximum);
    assert(tree.find_root_child(0) == zero);
    assert(tree.find_root_child(maximum) == maximum);
    assert(tree.find_child(zero, 10) == first);
    assert(tree.parent(first) == zero);
    assert(!tree.parent(zero).has_value());
    assert(tree.child_count(first) == 2);
    assert(tree.is_leaf(leaf));

    std::vector<dwpdsim::NodeId> segment;
    tree.resolve_segment(leaf, segment);
    assert((segment == std::vector<dwpdsim::NodeId>{20, 30}));
    assert(tree.segment_top(leaf) == 20);
    assert(tree.segment_leaf_for(second) == leaf);
    assert(tree.segment_leaf_for(first) == first);

    tree.record_access(second, 10, false);
    tree.record_access(second, 20, true);
    const dwpdsim::Node& node = tree.node(second);
    assert(node.access_count == 2);
    assert(node.first_seen_timestamp_ns == 10);
    assert(node.last_access_timestamp_ns == 20);
    assert(node.has_last_hit);
    assert(node.last_hit_timestamp_ns == 20);

    assert(tree.detach_leaf(leaf) == second);
    tree.release_detached(leaf);
    assert(tree.detach_leaf(second) == first);
    tree.release_detached(second);
    assert(!tree.contains(second));
    tree.resolve_segment(branch_leaf, segment);
    assert((segment == std::vector<dwpdsim::NodeId>{0, 10, 40}));

    const auto [interloper, interloper_created] = tree.get_or_create(first, 99, 40);
    const auto [recreated, recreated_created] = tree.get_or_create(first, 20, 50);
    assert(interloper_created);
    assert(interloper == 99);
    assert(recreated_created);
    assert(recreated == second);
    assert(tree.node(recreated).access_count == 0);
    assert(tree.node(recreated).first_seen_timestamp_ns == 50);
    assert(tree.size() == 6);

    const auto legacy_tree_copy = tree;
    dwpdsim::RadixTree assigned_tree;
    assigned_tree = legacy_tree_copy;
    assigned_tree.node(first).in_memory = true;
    assert(!tree.node(first).in_memory);
    assert(assigned_tree.parent(first) == tree.parent(first));

    tree.node(first).hybrid.ensure().components[1].in_dram = true;
    auto hybrid_tree_copy = tree;
    hybrid_tree_copy.node(first).hybrid->components[1].in_dram = false;
    assert(tree.node(first).hybrid->components[1].in_dram);
    assigned_tree = hybrid_tree_copy;
    assigned_tree.node(first).hybrid->components[1].in_dram = true;
    assert(!hybrid_tree_copy.node(first).hybrid->components[1].in_dram);
    static_assert(std::is_same_v<
        decltype(std::as_const(tree).node(first).hybrid.operator->()),
        const dwpdsim::HybridNodeState*>);

    return 0;
}
