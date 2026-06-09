"""The CodeGraph projection applier — code-delta → graph mutation."""

from __future__ import annotations

from typing import Any

import rustworkx as rx

from tyo3.graph.models import EdgeData, EdgeKind, SymbolNode
from tyo3.models.analysis import Range
from tyo3.models.navigation import ReferenceRole
from tyo3.models.symbols import SymbolKind


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
        if payload is None:
            return None
        if isinstance(payload, Range):
            return payload
        return Range.model_validate(payload)

    def _node_from_code_delta(self, n: dict[str, Any]) -> SymbolNode:
        """Build a ``SymbolNode`` from one ``CodeNodeDto`` dict."""
        return SymbolNode(
            durable_id=n["durable_id"],
            name=n["name"],
            qualified_name=n["qualified_name"],
            kind=SymbolKind(n["kind"]),
            file=n["file"],
            range=self._coerce_range(n["range"]),
            selection_range=self._coerce_range(n.get("name_range")),
            content_hash=n.get("content_hash"),
            content_hashes=dict(n.get("content_hashes") or {}),
            external=bool(n.get("external", False)),
            package=n.get("package"),
        )

    def apply_code_delta(self, code_delta: Any) -> None:
        """Apply a native ``CodeDelta`` to this graph — a **pure** function of the
        graph and the delta.

        Makes **no FFI calls** and touches **no session or snapshot**: it consumes
        only the delta's node/edge upserts, removals, and moves. This is the
        applier the parity oracle exercises in Phase 2 and that becomes the sole
        graph-update path at the Phase 4 cutover.

        *code_delta* is a pythonized ``CodeDeltaDto`` dict (the shape the native
        ``full_code_delta()`` / commit delta produce).

        **Revision-gating.** An incremental delta whose ``revision`` does not
        advance the graph (``<= self._revision``) is a stale/duplicate and is a
        **no-op** — publication is the commit tail (§5.3), so in-order is the
        rule and gating only makes the out-of-order case safe rather than
        corrupting the graph. A ``rescan`` delta is an *authoritative wholesale
        replacement* (it carries every node/edge — i.e. it is a full delta) and
        is therefore never gated out; it clears the graph and applies the
        complete set.
        """
        self._assert_mutable()
        d = code_delta if isinstance(code_delta, dict) else dict(code_delta)

        revision = d.get("revision")
        rescan = bool(d.get("rescan"))

        # Revision-gate: a stale/duplicate *incremental* delta is a no-op. A
        # rescan/full delta is authoritative and is applied regardless.
        if not rescan and revision is not None and self._revision is not None and revision <= self._revision:
            return

        # A rescan/full delta is the complete set — clear the graph and apply it
        # wholesale. (``nodes_upserted`` + ``edges_added`` carry every node/edge;
        # this is what ``full_code_delta()`` emits.) On a fresh graph this is a
        # no-op clear; on a populated one it replaces it.
        if rescan:
            self._graph = rx.PyDiGraph()
            self._id_to_index.clear()
            self._file_to_nodes.clear()
            self._file_to_edges.clear()
            self._file_importers.clear()
            self._semantic_subgraph_cache.clear()

        # 1. Removals first (so a remove+re-add of the same id is well-defined).
        removed_ids = list(d.get("nodes_removed") or [])
        if removed_ids:
            doomed = [self._id_to_index[i] for i in removed_ids if i in self._id_to_index]
            if doomed:
                self._graph.remove_nodes_from(doomed)
                self._rebuild_indexes()

        # 2. Node upserts. Re-emit (same id, new payload) replaces in place;
        #    new ids are added.
        for n in d.get("nodes_upserted") or []:
            node = self._node_from_code_delta(n)
            existing = self._id_to_index.get(node.durable_id)
            if existing is None:
                self._add_node(node)
            else:
                old = self._graph[existing]
                self._graph[existing] = node
                if old.file != node.file:
                    if existing in self._file_to_nodes.get(old.file, []):
                        self._file_to_nodes[old.file].remove(existing)
                    self._file_to_nodes[node.file].append(existing)
                self._semantic_subgraph_cache.clear()

        # 3. Moves: same id, unchanged body, new location only.
        for m in d.get("nodes_moved") or []:
            idx = self._id_to_index.get(m["durable_id"])
            if idx is None:
                continue
            node = self._graph[idx]
            updated = node.model_copy(
                update={
                    "file": m["file"],
                    "range": self._coerce_range(m["range"]),
                    "selection_range": self._coerce_range(m.get("name_range")),
                }
            )
            self._graph[idx] = updated
            if node.file != updated.file:
                if idx in self._file_to_nodes.get(node.file, []):
                    self._file_to_nodes[node.file].remove(idx)
                self._file_to_nodes[updated.file].append(idx)

        # 4. Edge removals then additions.
        for e in d.get("edges_removed") or []:
            self._remove_code_edge(e)
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

    def _remove_code_edge(self, e: dict[str, Any]) -> None:
        """Remove the first matching ``CodeEdgeDto`` edge.

        Symmetrically prunes the file-level reverse-dependency index
        ``_file_importers`` when the removed edge was the *last* IMPORTS edge
        between two project files (mirrors the maintenance in ``_add_code_edge``).
        """
        src_idx = self._id_to_index.get(e["source_id"])
        tgt_idx = self._id_to_index.get(e["destination_id"])
        if src_idx is None or tgt_idx is None:
            return
        kind = EdgeKind(e["kind"])
        want_range = self._coerce_range(e.get("range"))
        want_role = ReferenceRole(e["role"]) if e.get("role") else None
        src_node = self._graph[src_idx]
        tgt_node = self._graph[tgt_idx]
        for ei in self._graph.incident_edges(src_idx):
            s2, t2 = self._graph.get_edge_endpoints_by_index(ei)
            if s2 != src_idx or t2 != tgt_idx:
                continue
            data = self._graph.get_edge_data_by_index(ei)
            if data.kind == kind and data.file == e.get("file") and data.range == want_range and data.role == want_role:
                # Remove THIS specific (possibly parallel) edge by index —
                # ``remove_edge(src, tgt)`` would drop an arbitrary parallel edge
                # between the endpoints, corrupting multi-import/multi-ref pairs.
                self._graph.remove_edge_from_index(ei)
                self._semantic_subgraph_cache.clear()
                # Prune the reverse-dep index iff this was the last IMPORTS edge
                # connecting src.file → tgt.file (parallel imports may remain).
                if (
                    kind == EdgeKind.IMPORTS
                    and src_node.file != "<external>"
                    and tgt_node.file != "<external>"
                    and src_node.file != tgt_node.file
                    and not self._has_import_edge_between(src_node.file, tgt_node.file)
                ):
                    self._file_importers.get(tgt_node.file, set()).discard(src_node.file)
                return
