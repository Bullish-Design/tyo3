"""Export CodeGraph to various formats.

Provides DOT (Graphviz) and JSON serialization for visualization
and interchange.
"""

from __future__ import annotations

from typing import Any, TypedDict

from tyo3.graph.models import EdgeData, EdgeKind, SymbolNode

# ── DOT export ────────────────────────────────────────────


def to_dot(graph: Any, *, max_nodes: int | None = None) -> str:
    """Export the graph in DOT format for Graphviz visualization.

    Args:
        graph: A ``CodeGraph`` instance.
        max_nodes: If set, limit output to the first *max_nodes* nodes.
                   Useful for large graphs where the full DOT would be
                   unwieldy.

    Returns a DOT-format string suitable for ``dot``, ``neato``, etc.
    """
    g = graph.graph
    lines = ["digraph CodeGraph {", "  rankdir=LR;", "  node [shape=box];"]

    count = 0
    for idx in g.node_indices():
        if max_nodes is not None and count >= max_nodes:
            break
        count += 1
        node: SymbolNode = g[idx]
        label = _dot_label(node)
        color = _kind_color(str(node.kind.value))
        style = "dashed" if node.external else "solid"
        escaped_label = label.replace('"', '\\"')
        lines.append(
            f'  n{idx} [label="{escaped_label}", '
            f'color="{color}", style="{style}", fontname="monospace"];'
        )

    count = 0
    for edge_idx in g.edge_indices():
        if max_nodes is not None and count >= max_nodes * 3:
            break
        count += 1
        src, tgt = g.get_edge_endpoints_by_index(edge_idx)
        if max_nodes is not None and (src >= max_nodes or tgt >= max_nodes):
            continue
        data: EdgeData = g.get_edge_data_by_index(edge_idx)
        kind = data.kind.value
        style, color = _edge_style(kind)
        label = kind if kind != "references" else ""
        attr = f'label="{label}" color="{color}" style="{style}"'
        lines.append(f"  n{src} -> n{tgt} [{attr}];")

    lines.append("}")
    return "\n".join(lines)


def _dot_label(node: SymbolNode) -> str:
    """Build a human-readable DOT label for a symbol node."""
    suffix = ""
    if node.signature:
        suffix = f"\\n{_escape_dot(node.signature)}"
    prefix = f"[{node.package}] " if node.external and node.package else ""
    return f"{prefix}{node.name}{suffix}"


def _escape_dot(text: str) -> str:
    """Escape characters that DOT treats specially."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _kind_color(kind: str) -> str:
    """Map SymbolKind value to a color for DOT rendering."""
    colors: dict[str, str] = {
        "module": "#1f77b4",       # blue
        "class_": "#d62728",       # red
        "class": "#d62728",        # red
        "function": "#2ca02c",     # green
        "method": "#2ca02c",       # green
        "variable": "#7f7f7f",     # gray
        "constant": "#ff7f0e",     # orange
        "parameter": "#bcbd22",    # olive
        "property": "#9467bd",     # purple
        "constructor": "#d62728",  # red
        "enum_member": "#17becf",  # cyan
        "interface": "#e377c2",    # pink
        "module": "#1f77b4",       # blue (dup)
    }
    return colors.get(kind, "#333333")


def _edge_style(kind: str) -> tuple[str, str]:
    """Return (style, color) for an edge kind."""
    styles: dict[str, tuple[str, str]] = {
        "defines": ("bold", "#1f77b4"),
        "contains": ("solid", "#1f77b4"),
        "references": ("dashed", "#999999"),
        "imports": ("dotted", "#ff7f0e"),
        "inherits": ("bold", "#d62728"),
        "overrides": ("solid", "#d62728"),
        "type_of": ("dotted", "#2ca02c"),
        "returns": ("dotted", "#2ca02c"),
        "instantiates": ("dashed", "#9467bd"),
    }
    return styles.get(kind, ("solid", "#333333"))


# ── JSON export ───────────────────────────────────────────


class JsonNode(TypedDict):
    idx: int
    data: dict[str, Any]


class JsonEdge(TypedDict):
    src: int
    tgt: int
    data: dict[str, Any] | None


def to_json(graph: Any) -> dict[str, Any]:
    """Export the graph as a JSON-serialisable dictionary.

    Returns a dictionary with keys ``"nodes"`` and ``"edges"`` that
    can be passed to ``json.dumps``.

    Node data is produced via ``SymbolNode.model_dump(mode="json")``.
    Edge data is converted to a dict via ``dataclasses.asdict``.  Null
    edge data (from dependency graphs) becomes ``null``.
    """
    g = graph.graph
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for idx in g.node_indices():
        node: SymbolNode = g[idx]
        nodes.append({"idx": idx, "data": node.model_dump(mode="json")})

    for edge_idx in g.edge_indices():
        src, tgt = g.get_edge_endpoints_by_index(edge_idx)
        raw = g.get_edge_data_by_index(edge_idx)
        edge_data: dict[str, Any] | None = None
        if raw is not None:
            edge_data = _edge_to_dict(raw)
        edges.append({"src": src, "tgt": tgt, "data": edge_data})

    return {"nodes": nodes, "edges": edges}


def _edge_to_dict(edge: EdgeData) -> dict[str, Any]:
    """Convert an EdgeData to a JSON-safe dict."""
    from tyo3.models.analysis import Range

    d: dict[str, Any] = {"kind": edge.kind.value}
    if edge.file is not None:
        d["file"] = edge.file
    if edge.role is not None:
        d["role"] = edge.role.value
    if edge.range is not None:
        d["range"] = {
            "start": {"line": edge.range.start.line, "column": edge.range.start.column},
            "end": {"line": edge.range.end.line, "column": edge.range.end.column},
        }
    return d
