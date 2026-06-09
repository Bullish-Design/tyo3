"""AuthoredLayerView — an authored layer's revision-pinned view (§5.1).

Wraps ``Snapshot.authored()`` for one declared authored layer. ``ids()``
returns ids with a record at R; ``value(id)`` returns an ``AuthoredValue``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from tyo3.layers.base import LayerDiff

if TYPE_CHECKING:
    from tyo3.config import LayerConfig
    from tyo3.models.authored import AuthoredValue
    from tyo3.session import Snapshot


class AuthoredLayerView:
    """Revision-pinned view of one authored layer."""

    origin: Literal["authored"] = "authored"

    def __init__(self, snapshot: Snapshot, name: str, config: LayerConfig) -> None:
        self._snapshot = snapshot
        self.name = name
        self._config = config

    def ids(self) -> Iterable[str]:
        """DurableIds with an authored record at the pinned revision.

        Enumerates from the snapshot's captured authored store. If the
        native snapshot doesn't expose an enumeration, falls back to a
        code-graph-entity scan.
        """
        # Try the native enumeration accessor first (added by Step 4/5 hooks).
        native = self._snapshot._inner
        if hasattr(native, "authored_ids"):
            # Fall back to the scan only when enumeration is genuinely
            # unsupported; a real backend failure must surface, not be hidden
            # behind a silent O(N) scan (typed failure ≠ absence, V1 §5.12).
            try:
                return native.authored_ids(self.name)
            except (AttributeError, NotImplementedError):
                pass

        # Fallback: scan entity ids from the pinned graph and check each.
        # This is less efficient but correct — it reads-at-R.
        graph = self._snapshot.graph()
        from tyo3.graph.identity import is_entity_durable_id

        ids = []
        for idx in graph._graph.node_indices():
            node = graph._graph[idx]
            if not is_entity_durable_id(node.durable_id):
                continue
            # Absence is reported as ``status == "absent"``, not an exception; a
            # backend/format failure propagates (it is not silently skipped).
            val = self._snapshot.authored(self.name, node.durable_id)
            if val.status != "absent":
                ids.append(node.durable_id)
        return ids

    def value(self, durable_id: str) -> AuthoredValue | None:
        """The ``AuthoredValue`` at the pinned revision, or ``None`` if no record
        is present (typed *absence*).

        A backend or format failure is **not** absence — it propagates as the
        typed error raised by the snapshot read (V1 §5.12) and is never swallowed
        into ``None``.
        """
        val = self._snapshot.authored(self.name, durable_id)
        if val.status == "absent":
            return None
        return val

    def diff(self, other: AuthoredLayerView) -> LayerDiff:
        """Authored-layer diff between *other* (before) and *self* (after).

        Compares per-id authored values at R0 vs R1. The detailed
        ``AuthoredDiff`` (Step 5) provides more granularity; this is the
        uniform protocol view.
        """
        after_ids = set(self.ids())
        before_ids = set(other.ids())

        added = frozenset(after_ids - before_ids)
        removed = frozenset(before_ids - after_ids)
        # "drifted" in authored terms = same id present at both, value differs.
        drifted = frozenset()
        for did in after_ids & before_ids:
            # Both ids are present in their respective views, so these reads
            # resolve; a genuine read failure propagates (V1 §5.12).
            a_val = self._snapshot.authored(self.name, did)
            b_val = other._snapshot.authored(other.name, did)
            if a_val.value != b_val.value or a_val.revision != b_val.revision:
                drifted = drifted | {did}

        return LayerDiff(
            layer=self.name,
            added=added,
            removed=removed,
            drifted=drifted,
        )
