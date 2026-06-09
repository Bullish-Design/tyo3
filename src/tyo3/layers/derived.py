"""DerivedLayerView — a derived layer's revision-pinned view (§5.1).

Wraps ``Snapshot.derived()`` for one declared derived layer. ``ids()``
returns entity ids the layer ``applies_to`` at R (from the pinned graph
filtered by ``entity_kinds``); ``value(id)`` returns a ``DerivedValue``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from tyo3.graph.identity import is_entity_durable_id
from tyo3.layers.base import LayerDiff

if TYPE_CHECKING:
    from tyo3.config import LayerConfig
    from tyo3.models.derived import DerivedValue
    from tyo3.session import Snapshot


class DerivedLayerView:
    """Revision-pinned view of one derived layer."""

    origin: Literal["derived"] = "derived"

    def __init__(self, snapshot: Snapshot, name: str, config: LayerConfig) -> None:
        self._snapshot = snapshot
        self.name = name
        self._config = config

    def ids(self) -> Iterable[str]:
        """Entity DurableIds this layer applies to at the pinned revision.

        Reads the pinned graph and filters by ``entity_kinds``.
        """
        graph = self._snapshot.graph()
        entity_kinds = frozenset(self._config.entity_kinds) if self._config.entity_kinds else None

        for idx in graph._graph.node_indices():
            node = graph._graph[idx]
            if not is_entity_durable_id(node.durable_id):
                continue
            if entity_kinds is not None and node.kind.value not in entity_kinds:
                continue
            yield node.durable_id

    def value(self, durable_id: str) -> DerivedValue:
        """The ``DerivedValue`` at the pinned revision.

        Absence (id absent at R, or not applicable to this layer) is reported
        *in the value* as ``status == "absent"`` by the snapshot read — never as
        ``None`` and never as an exception. A backend/store/generator failure
        propagates as the typed error raised by the snapshot read (V1 §5.12); it
        is never swallowed into ``None``.
        """
        return self._snapshot.derived(self.name, durable_id)

    def diff(self, other: DerivedLayerView) -> LayerDiff:
        """Derived-layer diff between *other* (before) and *self* (after).

        Compares the cache key (input_hash) per id from each pinned graph.
        Drift means the profile hash changed — a recompute would occur.
        Reads only the two pinned graphs; no artifact or vector-store access.
        """
        after_graph = self._snapshot.graph()
        before_graph = other._snapshot.graph()

        after_ids = set(self.ids())
        before_ids = set(other.ids())

        # Cache key = content_hashes[hash_profile], taken from each pinned graph node.
        hash_profile = self._config.hash_profile or "structure"

        def _cache_key(graph, did: str) -> str | None:
            node = graph.symbol(did)
            if node is None:
                return None
            return node.content_hashes.get(hash_profile) or node.content_hash

        added = frozenset(after_ids - before_ids)
        removed = frozenset(before_ids - after_ids)
        drifted = frozenset(
            did for did in (after_ids & before_ids) if _cache_key(after_graph, did) != _cache_key(before_graph, did)
        )

        return LayerDiff(
            layer=self.name,
            added=added,
            removed=removed,
            drifted=drifted,
        )

    def _input_hash_for(self, durable_id: str) -> str | None:
        """The cache key (input_hash) for *durable_id* at this pinned revision."""
        graph = self._snapshot.graph()
        node = graph.symbol(durable_id)
        if node is None:
            return None
        hash_profile = self._config.hash_profile or "structure"
        return node.content_hashes.get(hash_profile) or node.content_hash
