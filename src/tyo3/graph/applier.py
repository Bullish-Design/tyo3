"""The CodeGraph projection applier — code-delta → graph mutation."""

from __future__ import annotations

from typing import Any

import rustworkx as rx

from tyo3.graph.models import EdgeData, EdgeKind, SymbolNode
from tyo3.models.analysis import Range
from tyo3.models.navigation import ReferenceRole
from tyo3.models.symbols import SymbolKind


def coerce_range(payload: Any) -> Range | None:
    """Validate a pythonized range dict (or ``None``) into a ``Range``."""
    if payload is None:
        return None
    if isinstance(payload, Range):
        return payload
    return Range.model_validate(payload)


def symbol_node_from_dto(n: dict[str, Any]) -> SymbolNode:
    """Build a ``SymbolNode`` from one ``CodeNodeDto`` dict.

    The ``CodeNodeDto`` shape is what the native ``full_code_delta()`` emits in
    its ``nodes_upserted`` list. Shared by the graph applier and by
    ``CodeLayerView``, which reads node data straight off the delta without
    building a ``CodeGraph`` (Project 31, #3).
    """
    return SymbolNode(
        durable_id=n["durable_id"],
        name=n["name"],
        qualified_name=n["qualified_name"],
        kind=SymbolKind(n["kind"]),
        file=n["file"],
        range=coerce_range(n["range"]),
        selection_range=coerce_range(n.get("name_range")),
        content_hash=n.get("content_hash"),
        content_hashes=dict(n.get("content_hashes") or {}),
        external=bool(n.get("external", False)),
        package=n.get("package"),
    )


class _ApplierMixin:
    """Pure native-delta applier methods for :class:`CodeGraph`."""

    def _add_node(self, node: SymbolNode) -> int:
        """Add a SymbolNode to the graph and update indexes."""
        self._assert_mutable()
        if node.durable_id in self._id_to_index:
            return self._id_to_index[node.durable_id]
        idx = self._graph.add_node(node)
        self._id_to_index[node.durable_id] = idx
        self._file_to_nodes[node.file].append(idx)
        self._semantic_subgraph_cache.clear()
        return idx

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        data: EdgeData,
        file: str,
    ) -> int | None:
        """Add an edge between two symbols by their IDs."""
        self._assert_mutable()
        src_idx = self._id_to_index.get(source_id)
        tgt_idx = self._id_to_index.get(target_id)
        if src_idx is None or tgt_idx is None:
            return None
        edge_idx = self._graph.add_edge(src_idx, tgt_idx, data)
        self._file_to_edges[file].append(edge_idx)
        self._semantic_subgraph_cache.clear()
        return edge_idx

    @staticmethod
    def _coerce_range(payload: Any) -> Range | None:
        """Validate a pythonized range dict (or ``None``) into a ``Range``."""
        return coerce_range(payload)

    def _node_from_code_delta(self, n: dict[str, Any]) -> SymbolNode:
        """Build a ``SymbolNode`` from one ``CodeNodeDto`` dict."""
        return symbol_node_from_dto(n)

    def apply_code_delta(self, code_delta: Any) -> None:
        """Build this graph from a full native ``CodeDelta`` — a **pure** function
        of the graph and the delta.

        Makes **no FFI calls** and touches **no session or snapshot**: it consumes
        only the delta's node/edge set. The only delta shape produced now is the
        full ``rescan`` delta from ``full_code_delta()`` (every graph is built on
        demand — Project 31, #1b), so the applier is an *authoritative wholesale
        replacement*: it clears the graph and applies the complete
        ``nodes_upserted`` + ``edges_added`` set. The incremental machinery
        (revision-gating, node/edge removals, in-place re-emit, moves) was retired
        with the per-commit incremental path (#1).

        *code_delta* is a pythonized ``CodeDeltaDto`` dict (the shape the native
        ``full_code_delta()`` produces). Secondary indexes (``_file_to_nodes``,
        ``_file_to_edges``, ``_file_importers``) are rebuilt inline as the fresh
        nodes/edges are added.
        """
        self._assert_mutable()
        d = code_delta if isinstance(code_delta, dict) else dict(code_delta)
        revision = d.get("revision")

        # Wholesale replacement: clear the graph + every secondary index, then
        # apply the complete node/edge set. On a fresh graph the clear is a no-op.
        self._graph = rx.PyDiGraph()
        self._id_to_index.clear()
        self._file_to_nodes.clear()
        self._file_to_edges.clear()
        self._file_importers.clear()
        self._semantic_subgraph_cache.clear()

        for n in d.get("nodes_upserted") or []:
            self._add_node(self._node_from_code_delta(n))
        for e in d.get("edges_added") or []:
            self._add_code_edge(e)

        self._semantic_subgraph_cache.clear()
        if revision is not None:
            self._revision = revision

    def _add_code_edge(self, e: dict[str, Any]) -> None:
        """Add one ``CodeEdgeDto`` as an ``EdgeData`` edge."""
        data = EdgeData(
            kind=EdgeKind(e["kind"]),
            file=e.get("file"),
            range=self._coerce_range(e.get("range")),
            role=ReferenceRole(e["role"]) if e.get("role") else None,
        )
        self._add_edge(e["source_id"], e["destination_id"], data, e.get("file") or "")
        # Maintain the file-level reverse-dependency index from IMPORTS edges
        # (project→project only), mirroring _add_import_edge.
        if data.kind == EdgeKind.IMPORTS:
            src = self.symbol(e["source_id"])
            tgt = self.symbol(e["destination_id"])
            if (
                src is not None
                and tgt is not None
                and src.file != "<external>"
                and tgt.file != "<external>"
                and src.file != tgt.file
            ):
                self._file_importers[tgt.file].add(src.file)
