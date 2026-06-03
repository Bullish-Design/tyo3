"""Shared graph test helpers — avoid duplication across test modules.

Import this from test files to get ``find_one()`` and ``edges_of_kind()``.
These are helper utilities, not test cases.
"""

from __future__ import annotations

from tyo3.graph import CodeGraph, EdgeKind
from tyo3.graph.models import SymbolNode
from tyo3.models.symbols import SymbolKind


def find_one(
    graph: CodeGraph,
    *,
    file_suffix: str,
    name: str,
    kind: SymbolKind,
) -> SymbolNode:
    """Find exactly one non-external symbol matching the criteria.

    Uses ``file.endswith(file_suffix)`` to keep tests working while
    paths are still absolute.  After path normalization (Phase 7),
    update callers to use exact relative paths.
    """
    matches = [
        node
        for node in graph.symbols_of_kind(kind)
        if node.file.endswith(file_suffix)
        and node.name == name
        and not node.external
    ]
    assert len(matches) == 1, (
        f"Expected exactly one {kind} named {name!r} in *{file_suffix}, "
        f"got {[m.symbol_id for m in matches]}"
    )
    return matches[0]


def edges_of_kind(
    graph: CodeGraph, kind: EdgeKind
) -> list[tuple[str, str]]:
    """Return all ``(source_symbol_id, target_symbol_id)`` pairs for edges of *kind*."""
    result: list[tuple[str, str]] = []
    for edge_idx in graph.graph.edge_indices():
        data = graph.graph.get_edge_data_by_index(edge_idx)
        if data.kind != kind:
            continue
        src, tgt = graph.graph.get_edge_endpoints_by_index(edge_idx)
        result.append((graph.graph[src].symbol_id, graph.graph[tgt].symbol_id))
    return result
