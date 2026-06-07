"""CodeLayerView — the code layer's revision-pinned view (§5.1).

Wraps ``Snapshot.graph()`` for one pinned revision. ``ids()`` returns
entity DurableIds (excluding ``<module>`` / ``<external>`` synthetics);
``value(id)`` returns the ``SymbolNode``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Literal

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

        Returns a ``LayerDiff`` — the uniform protocol view. For the
        detailed ``CodeDiff`` (with moved, edges), use :func:`SnapshotDiff`
        (the combined diff) or :func:`_compute_code_diff` directly.
        """
        from tyo3.models.diff import _compute_code_diff

        cd = _compute_code_diff(self._snapshot, other._snapshot)
        return LayerDiff(
            layer="code",
            added=cd.added | cd.changed,
            removed=cd.removed,
            drifted=frozenset(),  # code layer has no drift concept
        )
