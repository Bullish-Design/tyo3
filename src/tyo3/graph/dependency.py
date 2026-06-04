"""Cached dependency graphs for external packages."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import rustworkx as rx

from tyo3.graph.models import EdgeData, EdgeKind, SymbolNode
from tyo3.models.navigation import ReferenceRole
from tyo3.models.symbols import SymbolKind

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".cache" / "tyo3" / "deps"


class DependencyGraph:
    """Cached code graph for an external package.

    Stores a pre-built symbol graph for a dependency (stdlib,
    pydantic, requests, etc.) so that cross-package queries can
    be answered without re-indexing every session.
    """

    def __init__(
        self,
        package: str,
        version: str,
        graph: rx.PyDiGraph,
        id_to_index: dict[str, int],
    ) -> None:
        self.package = package
        self.version = version
        self.graph = graph
        self._id_to_index = id_to_index

    # ── Queries ───────────────────────────────────────────

    def lookup(self, symbol_id: str) -> SymbolNode | None:
        """Find a symbol in this dependency graph."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return None
        return self.graph[idx]

    def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
        """All symbols of a given kind in this dependency."""
        return [
            self.graph[idx]
            for idx in self.graph.node_indices()
            if self.graph[idx].kind == kind
        ]

    def all_symbols(self) -> list[SymbolNode]:
        """All symbols in this dependency graph."""
        return [self.graph[i] for i in self.graph.node_indices()]

    # ── Persistence ───────────────────────────────────────

    def save(self) -> Path:
        """Cache this dependency graph to disk.

        Returns the path to the saved file.
        """
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = CACHE_DIR / f"{self.package}-{self.version}.json"

        nodes = []
        for idx in self.graph.node_indices():
            node: SymbolNode = self.graph[idx]
            nodes.append({"idx": idx, "data": node.model_dump(mode="json")})

        edges = []
        for edge_idx in self.graph.edge_indices():
            src, tgt = self.graph.get_edge_endpoints_by_index(edge_idx)
            raw = self.graph.get_edge_data_by_index(edge_idx)
            edge_dict: dict[str, Any] = {
                "src_id": self.graph[src].symbol_id,
                "tgt_id": self.graph[tgt].symbol_id,
            }
            if raw is not None:
                edge_data_dict: dict[str, str] = {"kind": raw.kind.value}
                if raw.file:
                    edge_data_dict["file"] = raw.file
                if raw.role:
                    edge_data_dict["role"] = raw.role.value
                edge_dict["data"] = edge_data_dict
            edges.append(edge_dict)

        data = {
            "package": self.package,
            "version": self.version,
            "nodes": nodes,
            "edges": edges,
        }
        cache_path.write_text(json.dumps(data, indent=2))
        logger.info("Cached dependency graph: %s (%d nodes, %d edges)",
                      cache_path, len(nodes), len(edges))
        return cache_path

    @classmethod
    def load(cls, package: str, version: str) -> DependencyGraph | None:
        """Load a cached dependency graph from disk.

        Returns None if no cache exists or it fails to load.
        """
        cache_path = CACHE_DIR / f"{package}-{version}.json"
        if not cache_path.exists():
            return None

        try:
            data = json.loads(cache_path.read_text())
            graph: rx.PyDiGraph = rx.PyDiGraph()
            id_to_index: dict[str, int] = {}

            for node_data in data["nodes"]:
                node = SymbolNode.model_validate(node_data["data"])
                idx = graph.add_node(node)
                id_to_index[node.symbol_id] = idx

            for edge_data in data.get("edges", []):
                # New format: symbol_id-based
                src_idx = id_to_index.get(edge_data.get("src_id"))
                tgt_idx = id_to_index.get(edge_data.get("tgt_id"))
                if src_idx is not None and tgt_idx is not None:
                    raw_edge = edge_data.get("data")
                    if raw_edge is not None:
                        edge_obj = EdgeData(
                            kind=EdgeKind(raw_edge["kind"]),
                            file=raw_edge.get("file"),
                            role=ReferenceRole(role_raw) if (role_raw := raw_edge.get("role")) is not None else None,
                        )
                    else:
                        edge_obj = None
                    graph.add_edge(src_idx, tgt_idx, edge_obj)
                    continue

                # Fallback for old format ("src"/"tgt" with integer values)
                if "src_id" not in edge_data and "src" in edge_data:
                    src_val, tgt_val = edge_data["src"], edge_data["tgt"]
                    if src_val < graph.num_nodes() and tgt_val < graph.num_nodes():
                        graph.add_edge(src_val, tgt_val, None)

            return cls(package, version, graph, id_to_index)
        except Exception:
            logger.warning(
                "Failed to load cached dependency graph for %s-%s",
                package, version, exc_info=True,
            )
            return None

    @classmethod
    def list_cached(cls) -> list[tuple[str, str]]:
        """List all cached dependency graphs as (package, version) pairs."""
        if not CACHE_DIR.exists():
            return []
        results = []
        for p in CACHE_DIR.glob("*.json"):
            name = p.stem  # e.g. "stdlib-unknown" or "pydantic-2.7.0"
            if "-" in name:
                # Split on last hyphen: package may contain hyphens
                parts = name.rsplit("-", 1)
                results.append((parts[0], parts[1]))
        return results
