#include <cassert>

#include "dwpdsim/radix_tree.hpp"

int main() {
    dwpdsim::RadixTree tree;

    const auto [first_a, created_a] = tree.get_or_create(dwpdsim::kRootNodeId, 1, 10);
    const auto [first_b, created_b] = tree.get_or_create(first_a, 2, 10);
    const auto [same_a, reused_a] = tree.get_or_create(dwpdsim::kRootNodeId, 1, 20);
    const auto [second_b, created_second_b] = tree.get_or_create(dwpdsim::kRootNodeId, 2, 20);
    const auto [nested_b, created_nested_b] = tree.get_or_create(second_b, 2, 20);

    assert(created_a);
    assert(created_b);
    assert(!reused_a);
    assert(created_second_b);
    assert(created_nested_b);
    assert(first_a == same_a);
    assert(first_b != nested_b);

    tree.record_access(first_a, 10, false);
    tree.record_access(first_a, 20, true);
    const dwpdsim::Node& node = tree.node(first_a);
    assert(node.access_count == 2);
    assert(node.first_seen_timestamp == 10);
    assert(node.last_access_timestamp == 20);
    assert(node.has_last_hit);
    assert(node.last_hit_timestamp == 20);
    assert(tree.size() == 5);

    return 0;
}
