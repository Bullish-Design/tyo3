"""Structural diff between two CodeGraph projections."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from tyo3.graph.models import EdgeData, EdgeDiff, EdgeKind, GraphDiff
from tyo3.models.analysis import Range

if TYPE_CHECKING:
    from tyo3.graph.projection import CodeGraph


class _DiffMixin:
    """Diff methods for :class:`CodeGraph`."""

    @staticmethod
    def _range_key(range_: Range | None) -> tuple[int, int, int, int] | None:
        if range_ is None:
            return None
        return (
            range_.start.line,
            range_.start.column,
            range_.end.line,
            range_.end.column,
        )

    def _edge_map(self) -> dict[tuple[Any, ...], EdgeDiff]:
        edges: dict[tuple[Any, ...], EdgeDiff] = {}
        counts: dict[tuple[Any, ...], int] = defaultdict(int)
        for edge_idx in self._graph.edge_indices():
            data = self._graph.get_edge_data_by_index(edge_idx)
            src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
            source_id = self._graph[src].durable_id
            target_id = self._graph[tgt].durable_id
            base_key = (
                source_id,
                target_id,
                data.kind,
                data.file,
                self._range_key(data.range),
                data.role,
            )
            ordinal = counts[base_key]
            counts[base_key] += 1
            key = (*base_key, ordinal)
            edges[key] = EdgeDiff(source_id, target_id, data)
        return edges

    def diff(self, before: CodeGraph) -> GraphDiff:
        """Return the structural graph delta from *before* to ``self``."""
        before_nodes = {before._graph[idx].durable_id: before._graph[idx] for idx in before._graph.node_indices()}
        after_nodes = {self._graph[idx].durable_id: self._graph[idx] for idx in self._graph.node_indices()}

        before_edges = before._edge_map()
        after_edges = self._edge_map()

        return GraphDiff(
            added_nodes=[after_nodes[did] for did in sorted(after_nodes.keys() - before_nodes.keys())],
            removed_nodes=[before_nodes[did] for did in sorted(before_nodes.keys() - after_nodes.keys())],
            added_edges=[after_edges[key] for key in sorted(after_edges.keys() - before_edges.keys(), key=repr)],
            removed_edges=[before_edges[key] for key in sorted(before_edges.keys() - after_edges.keys(), key=repr)],
        )

    def _edges_of_kind(
        self,
        durable_id: str,
        kinds: set[EdgeKind],
        *,
        incoming: bool = False,
    ) -> list[tuple[int, EdgeData]]:
        """Return all edges matching *kinds* for a node.

        When *incoming* is False (default), returns outgoing edges as
        ``(target_index, edge_data)`` pairs.
        When *incoming* is True, returns incoming edges as
        ``(source_index, edge_data)`` pairs.

        Uses RustworkX's ``in_edges`` / ``out_edges`` which return
        all parallel edges in a single efficient Rust-dide call.
        """
        idx = self._id_to_index.get(durable_id)
        if idx is None:
            return []
        result: list[tuple[int, EdgeData]] = []
        if incoming:
            for src, _tgt, data in self._graph.in_edges(idx):
                if data.kind in kinds:
                    result.append((src, data))
        else:
            for _src, tgt, data in self._graph.out_edges(idx):
                if data.kind in kinds:
                    result.append((tgt, data))
        return result
