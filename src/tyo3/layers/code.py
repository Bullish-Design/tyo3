"""CodeLayerView — the code layer's revision-pinned view (§5.1).

Reads node data straight off the snapshot's native ``full_code_delta()`` — the
same ``nodes_upserted`` list the graph applier consumes — rather than building a
``rustworkx`` ``CodeGraph`` just to iterate nodes (Project 31, #3). ``ids()``
returns entity DurableIds (excluding ``<module>`` / ``<external>`` synthetics);
``value(id)`` returns the ``SymbolNode``; ``diff()`` compares node id sets +
``content_hash`` across two snapshots — none of which needs a graph.

Real graph queries (``dependents`` / ``transitive_dependents`` / edges) still go
through ``snapshot.graph()`` (the on-demand build) — ``EntityView`` and the
precision refiner use that, not this view.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Literal

from tyo3.graph.applier import symbol_node_from_dto
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
        self._nodes: dict[str, dict[str, Any]] | None = None  # durable_id → CodeNodeDto

    @property
    def _node_index(self) -> dict[str, dict[str, Any]]:
        """Lazily-built ``durable_id → CodeNodeDto`` map for the pinned revision.

        Reads the snapshot's own frozen-database ``full_code_delta()`` and indexes
        its ``nodes_upserted`` — a pure read, no graph construction, no session
        state. Includes ``<module>`` / ``<external>`` synthetics; entity filtering
        happens in ``ids()`` / ``value()`` via :func:`is_entity_durable_id`.
        """
        if self._nodes is None:
            delta = self._snapshot._native().full_code_delta()
            self._nodes = {n["durable_id"]: n for n in (delta.get("nodes_upserted") or [])}
        return self._nodes

    def ids(self) -> Iterable[str]:
        """Entity DurableIds present at the pinned revision.

        Excludes ``<module>`` and ``<external>`` synthetics.
        """
        for did in self._node_index:
            if is_entity_durable_id(did):
                yield did

    def value(self, durable_id: str) -> SymbolNode | None:
        """The ``SymbolNode`` at the pinned revision, or ``None``.

        Built from the native ``CodeNodeDto`` via the shared
        :func:`symbol_node_from_dto`, so the model is identical to the one the
        graph would return.
        """
        n = self._node_index.get(durable_id)
        if n is None or not is_entity_durable_id(durable_id):
            return None
        return symbol_node_from_dto(n)

    def diff(self, other: CodeLayerView) -> LayerDiff:
        """Structural code diff between *other* (before) and *self* (after).

        Returns a ``LayerDiff`` — note that the code-layer diff carries more
        precision via ``CodeDiff`` (Step 3); this is the uniform protocol
        view for the combined diff.
        """
        cd = _code_layer_diff_detail(self, other)
        return LayerDiff(
            layer="code",
            added=cd.added | cd.changed,  # changed is "added" in generic terms
            removed=cd.removed,
            drifted=frozenset(),  # code layer has no drift concept yet
        )


def _code_layer_diff_detail(after: CodeLayerView, before: CodeLayerView) -> Any:
    """Compute the detailed CodeDiff (Step 3) — factored out for reuse.

    Node-only set/hash comparison over the two snapshots' native node lists; no
    graph is built (edges are out of scope for the code ``LayerView.diff``).
    """
    after_ids = {did for did in after._node_index if is_entity_durable_id(did)}
    before_ids = {did for did in before._node_index if is_entity_durable_id(did)}

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
        did for did in (after_ids & before_ids) if _content_hash(after, did) != _content_hash(before, did)
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
    """Get the content_hash for an entity id from a pinned snapshot's node list."""
    n = view._node_index.get(durable_id)
    return n.get("content_hash") if n is not None else None
