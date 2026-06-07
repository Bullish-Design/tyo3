"""CodeLayerView — the code layer's revision-pinned view (§5.1).

Wraps ``Snapshot.graph()`` for one pinned revision. ``ids()`` returns
entity DurableIds (excluding ``<module>`` / ``<external>`` synthetics);
``value(id)`` returns the ``SymbolNode``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Literal

from tyo3.graph.identity import is_entity_durable_id
from tyo3.layers.base import LayerDiff

if TYPE_CHECKING:
    from tyo3.graph.models import SymbolNode
    from tyo3.session import Snapshot


class CodeLayerView:
    """Revision-pinned view of the code layer."""

    name: str = "code"
    origin: Literal["code"] = "code"

    def __init__(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self._graph = None  # lazily built

    @property
    def graph(self):
        """The pinned code graph at this snapshot's revision."""
        if self._graph is None:
            self._graph = self._snapshot.graph()
        return self._graph

    def ids(self) -> Iterable[str]:
        """Entity DurableIds present in the pinned graph.

        Excludes ``<module>`` and ``<external>`` synthetics.
        """
        for idx in self.graph._graph.node_indices():
            node: SymbolNode = self.graph._graph[idx]
            if is_entity_durable_id(node.durable_id):
                yield node.durable_id

    def value(self, durable_id: str) -> SymbolNode | None:
        """The ``SymbolNode`` at the pinned revision, or ``None``."""
        node = self.graph.symbol(durable_id)
        if node is not None and is_entity_durable_id(node.durable_id):
            return node
        return None

    def diff(self, other: CodeLayerView) -> LayerDiff:
        """Structural code diff between *other* (before) and *self* (after).

        Returns a ``LayerDiff`` — note that the code-layer diff carries more
        precision via ``CodeDiff`` (Step 3); this is the uniform protocol
        view for the combined diff.
        """
        # Deferred import to avoid circularity; implemented in Step 3.
        from tyo3.models.diff import CodeDiff

        cd = _code_layer_diff_detail(self, other)
        return LayerDiff(
            layer="code",
            added=cd.added | cd.changed,  # changed is "added" in generic terms
            removed=cd.removed,
            drifted=frozenset(),  # code layer has no drift concept yet
        )


def _code_layer_diff_detail(after: CodeLayerView, before: CodeLayerView) -> Any:
    """Compute the detailed CodeDiff (Step 3) — factored out for reuse."""
    # Will be replaced by the full CodeDiff impl in Step 3.
    after_ids = {n.durable_id for idx in after.graph._graph.node_indices()
                 if is_entity_durable_id((n := after.graph._graph[idx]).durable_id)}
    before_ids = {n.durable_id for idx in before.graph._graph.node_indices()
                  if is_entity_durable_id((n := before.graph._graph[idx]).durable_id)}

    # Simple set delta for now — Step 3 enriches with changed/moved/edges.
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class CodeDiff:
        added: frozenset[str]
        removed: frozenset[str]
        changed: frozenset[str]
        moved: frozenset[str]
        edges_added: frozenset
        edges_removed: frozenset

    # Compare content_hash for ids in both sets.
    changed = frozenset(
        did for did in (after_ids & before_ids)
        if _content_hash(after, did) != _content_hash(before, did)
    )
    moved = frozenset()  # Step 3 will compute moved ids
    added = after_ids - before_ids
    removed = before_ids - after_ids

    return CodeDiff(
        added=added,
        removed=removed,
        changed=changed,
        moved=moved,
        edges_added=frozenset(),
        edges_removed=frozenset(),
    )


def _content_hash(view: CodeLayerView, durable_id: str) -> str | None:
    """Get the content_hash for an entity id from a pinned graph."""
    node = view.graph.symbol(durable_id)
    if node is not None:
        return node.content_hash
    return None
