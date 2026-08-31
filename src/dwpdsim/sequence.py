"""Block access history and forward-prefix indexing."""

from dataclasses import dataclass

from dwpdsim.models import BlockHistory, BlockId, SequenceNodeView, Timestamp

ROOT_NODE = 0


@dataclass(slots=True)
class _MutableHistory:
    first_seen_timestamp: Timestamp
    last_seen_timestamp: Timestamp
    access_count: int


@dataclass(slots=True)
class _MutableNode:
    node_id: int
    parent_id: int | None
    block_id: BlockId | None
    depth: int
    access_count: int = 0


class SequenceIndex:
    """Own block histories and one flat forward-prefix tree."""

    ROOT_NODE = ROOT_NODE

    def __init__(self) -> None:
        self._histories: dict[BlockId, _MutableHistory] = {}
        self._nodes: dict[int, _MutableNode] = {
            ROOT_NODE: _MutableNode(
                node_id=ROOT_NODE,
                parent_id=None,
                block_id=None,
                depth=0,
            )
        }
        self._children: dict[tuple[int, BlockId], int] = {}
        self._next_node_id = ROOT_NODE + 1

    def history(self, block_id: BlockId) -> BlockHistory | None:
        """Return an immutable history snapshot, or ``None`` if unseen."""

        history = self._histories.get(block_id)
        if history is None:
            return None
        return BlockHistory(
            first_seen_timestamp=history.first_seen_timestamp,
            last_seen_timestamp=history.last_seen_timestamp,
            access_count=history.access_count,
        )

    def node(self, node_id: int) -> SequenceNodeView:
        """Return an immutable prefix-node snapshot."""

        try:
            node = self._nodes[node_id]
        except KeyError as error:
            raise KeyError(f"unknown sequence node: {node_id}") from error
        return SequenceNodeView(
            node_id=node.node_id,
            parent_id=node.parent_id,
            block_id=node.block_id,
            depth=node.depth,
            access_count=node.access_count,
        )

    def existing_child(self, parent_node_id: int, block_id: BlockId) -> int | None:
        """Return an existing child node without changing the index."""

        self._require_node(parent_node_id)
        return self._children.get((parent_node_id, block_id))

    def observe(
        self,
        block_id: BlockId,
        timestamp: Timestamp,
        parent_node_id: int,
    ) -> int:
        """Record one input position and return its prefix node id."""

        parent = self._require_node(parent_node_id)
        child_id = self._children.get((parent_node_id, block_id))
        if child_id is None:
            child_id = self._next_node_id
            self._next_node_id += 1
            self._children[(parent_node_id, block_id)] = child_id
            self._nodes[child_id] = _MutableNode(
                node_id=child_id,
                parent_id=parent_node_id,
                block_id=block_id,
                depth=parent.depth + 1,
            )

        self._nodes[child_id].access_count += 1
        history = self._histories.get(block_id)
        if history is None:
            self._histories[block_id] = _MutableHistory(
                first_seen_timestamp=timestamp,
                last_seen_timestamp=timestamp,
                access_count=1,
            )
        else:
            history.last_seen_timestamp = timestamp
            history.access_count += 1
        return child_id

    def _require_node(self, node_id: int) -> _MutableNode:
        try:
            return self._nodes[node_id]
        except KeyError as error:
            raise KeyError(f"unknown sequence node: {node_id}") from error
