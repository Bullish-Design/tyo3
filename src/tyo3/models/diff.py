"""Diff models — CodeDiff, AuthoredDiff, LayerDiff, SnapshotDiff (§10.3).

All diffs are pure functions of two pinned ``Snapshot``s — no head access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tyo3.session import Snapshot


@dataclass(frozen=True)
class CodeDiff:
    """Structural code-layer diff between two pinned revisions.

    Node identity is the ``DurableId``; ``changed`` compares ``content_hash``;
    ``moved`` compares ``(file, qualified_name)`` with equal ``content_hash``.
    """

    added: frozenset[str]
    removed: frozenset[str]
    changed: frozenset[str]
    moved: frozenset[str]
    edges_added: frozenset[tuple]  # (src_id, dst_id, kind, role)
    edges_removed: frozenset[tuple]

    @property
    def is_empty(self) -> bool:
        return not (
            self.added
            or self.removed
            or self.changed
            or self.moved
            or self.edges_added
            or self.edges_removed
        )


@dataclass(frozen=True)
class AuthoredDiff:
    """Authored-layer diff between two pinned revisions.

    ``changed`` keys on version ``revision`` and/or value inequality.
    ``review_changed`` on derived status differing between R0 and R1.
    ``removed`` is rare (record did not yet exist at R0) — never deleted.
    """

    layer: str
    added: frozenset[str]
    changed: frozenset[str]
    review_changed: frozenset[str]
    removed: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.changed or self.review_changed or self.removed)


@dataclass(frozen=True)
class SnapshotDiff:
    """Combined, id-keyed diff unifying every layer's delta (§10.3).

    Pure function of two pinned snapshots — no head access.
    """

    before_revision: int
    after_revision: int
    code: CodeDiff
    derived: dict[str, Any]  # layer name → LayerDiff
    authored: dict[str, AuthoredDiff]

    def entities(self) -> frozenset[str]:
        """Union of all touched ``DurableId``s across every layer."""
        ids: set[str] = set()
        ids |= self.code.added | self.code.removed | self.code.changed | self.code.moved
        for d in self.derived.values():
            ids |= d.added | d.removed | d.drifted
        for a in self.authored.values():
            ids |= a.added | a.changed | a.review_changed | a.removed
        return frozenset(ids)

    @property
    def is_empty(self) -> bool:
        return (
            self.code.is_empty
            and all(d.is_empty for d in self.derived.values())
            and all(a.is_empty for a in self.authored.values())
        )

    @classmethod
    def compute(cls, after: Snapshot, before: Snapshot) -> SnapshotDiff:
        """Compute the combined diff from two pinned snapshots.

        *after* and *before* must be from the same session.  Raises
        ``ValueError`` if they are incompatible.
        """
        if before.revision > after.revision:
            raise ValueError(
                f"before.revision ({before.revision}) > after.revision ({after.revision})"
            )

        # Code diff
        code_diff = _compute_code_diff(after, before)

        # Derived diffs (in config topo order)
        derived: dict[str, Any] = {}
        if after._config is not None:
            for lname in after._config.topo_order:
                lcfg = after._config.layers.get(lname)
                if lcfg is None or lcfg.origin != "derived":
                    continue
                try:
                    after_view = after.layer(lname)
                    before_view = before.layer(lname)
                    from tyo3.layers.base import LayerDiff
                    derived[lname] = after_view.diff(before_view)
                except Exception:
                    continue

        # Authored diffs
        authored: dict[str, AuthoredDiff] = {}
        if after._config is not None:
            for lname, lcfg in after._config.layers.items():
                if lcfg.origin != "authored":
                    continue
                try:
                    ad = _compute_authored_diff(after, before, lname)
                    if not ad.is_empty:
                        authored[lname] = ad
                except Exception:
                    continue

        return cls(
            before_revision=before.revision,
            after_revision=after.revision,
            code=code_diff,
            derived=derived,
            authored=authored,
        )


def _compute_code_diff(after: Snapshot, before: Snapshot) -> CodeDiff:
    """Compute the detailed code diff from two pinned snapshots."""
    from tyo3.graph.identity import is_entity_durable_id

    after_graph = after.graph()
    before_graph = before.graph()

    after_nodes: dict[str, Any] = {}
    for idx in after_graph._graph.node_indices():
        node = after_graph._graph[idx]
        if is_entity_durable_id(node.durable_id):
            after_nodes[node.durable_id] = node

    before_nodes: dict[str, Any] = {}
    for idx in before_graph._graph.node_indices():
        node = before_graph._graph[idx]
        if is_entity_durable_id(node.durable_id):
            before_nodes[node.durable_id] = node

    after_ids = set(after_nodes.keys())
    before_ids = set(before_nodes.keys())

    added = frozenset(after_ids - before_ids)
    removed = frozenset(before_ids - after_ids)

    changed: set[str] = set()
    moved: set[str] = set()
    for did in after_ids & before_ids:
        a_node = after_nodes[did]
        b_node = before_nodes[did]
        if a_node.content_hash != b_node.content_hash:
            changed.add(did)
        elif a_node.file != b_node.file or a_node.qualified_name != b_node.qualified_name:
            # Same hash, different location — moved.
            moved.add(did)

    # Edge diff
    def _edge_set(graph) -> frozenset[tuple]:
        edges: set[tuple] = set()
        for edge_idx in graph._graph.edge_indices():
            data = graph._graph.get_edge_data_by_index(edge_idx)
            src, tgt = graph._graph.get_edge_endpoints_by_index(edge_idx)
            src_id = graph._graph[src].durable_id
            tgt_id = graph._graph[tgt].durable_id
            if not is_entity_durable_id(src_id) and not is_entity_durable_id(tgt_id):
                continue
            key = (src_id, tgt_id, data.kind.value, (data.role.value if data.role else None))
            edges.add(key)
        return frozenset(edges)

    after_edges = _edge_set(after_graph)
    before_edges = _edge_set(before_graph)

    return CodeDiff(
        added=added,
        removed=removed,
        changed=frozenset(changed),
        moved=frozenset(moved),
        edges_added=after_edges - before_edges,
        edges_removed=before_edges - after_edges,
    )


def _compute_authored_diff(after: Snapshot, before: Snapshot, layer: str) -> AuthoredDiff:
    """Compute the authored diff for one layer from two pinned snapshots."""
    after_view = after.layer(layer)
    before_view = before.layer(layer)

    after_ids = set(after_view.ids())
    before_ids = set(before_view.ids())

    added = frozenset(after_ids - before_ids)
    removed = frozenset(before_ids - after_ids)

    changed: set[str] = set()
    review_changed: set[str] = set()
    for did in after_ids & before_ids:
        try:
            a_val = after.authored(layer, did)
            b_val = before.authored(layer, did)

            # Value or revision changed → authored change.
            if a_val.revision != b_val.revision or a_val.value != b_val.value:
                changed.add(did)

            # Status changed (e.g. became needs_review/orphaned).
            if a_val.status != b_val.status:
                review_changed.add(did)
        except Exception:
            continue

    return AuthoredDiff(
        layer=layer,
        added=added,
        changed=frozenset(changed),
        review_changed=frozenset(review_changed),
        removed=removed,
    )
