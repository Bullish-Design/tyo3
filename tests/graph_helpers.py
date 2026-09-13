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
    file_suffix: str = "",
    file: str = "",
    name: str,
    kind: SymbolKind,
) -> SymbolNode:
    """Find exactly one non-external symbol matching the criteria.

    Prefer *file* (exact match) when graph paths are project-relative.
    Falls back to *file_suffix* (``endswith`` match) for backward
    compatibility or when paths are still absolute.
    """
    if file:
        matches = [
            node
            for node in graph.symbols_of_kind(kind)
            if node.file == file and node.name == name and not node.external
        ]
        tag = file
    else:
        matches = [
            node
            for node in graph.symbols_of_kind(kind)
            if node.file.endswith(file_suffix) and node.name == name and not node.external
        ]
        tag = f"*{file_suffix}"
    assert len(matches) == 1, (
        f"Expected exactly one {kind} named {name!r} in {tag}, got {[m.durable_id for m in matches]}"
    )
    return matches[0]


def edges_of_kind(graph: CodeGraph, kind: EdgeKind) -> list[tuple[str, str]]:
    """Return all ``(source_id, target_id)`` pairs for edges of *kind*."""
    result: list[tuple[str, str]] = []
    for edge_idx in graph.graph.edge_indices():
        data = graph.graph.get_edge_data_by_index(edge_idx)
        if data.kind != kind:
            continue
        src, tgt = graph.graph.get_edge_endpoints_by_index(edge_idx)
        result.append((graph.graph[src].durable_id, graph.graph[tgt].durable_id))
    return result
