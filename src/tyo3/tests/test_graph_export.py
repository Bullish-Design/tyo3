"""Tests for DOT and JSON export functions."""

from __future__ import annotations

from tyo3.graph import CodeGraph, EdgeData, EdgeKind, SymbolNode
from tyo3.graph.models import ReferenceRole
from tyo3.models.analysis import Range
from tyo3.models.symbols import SymbolKind


class TestExport:
    def _make_graph_with_data(self) -> CodeGraph:
        graph = CodeGraph()
        mod = SymbolNode(
            symbol_id="src/main.py::<module>",
            name="main",
            qualified_name="<module>",
            kind=SymbolKind.MODULE,
            file="src/main.py",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1},
                 "end": {"line": 1, "column": 1}}
            ),
        )
        graph._add_node(mod)
        func = SymbolNode(
            symbol_id="src/main.py::greet",
            name="greet",
            qualified_name="greet",
            kind=SymbolKind.FUNCTION,
            file="src/main.py",
            range=Range.model_validate(
                {"start": {"line": 3, "column": 1},
                 "end": {"line": 5, "column": 10}}
            ),
            signature="def greet(name: str) -> str",
        )
        graph._add_node(func)
        edge = EdgeData(kind=EdgeKind.DEFINES, file="src/main.py")
        graph._add_edge("src/main.py::<module>", "src/main.py::greet",
                        edge, "src/main.py")
        return graph

    def test_to_dot_non_empty(self) -> None:
        from tyo3.graph.export import to_dot
        graph = self._make_graph_with_data()
        dot = to_dot(graph)
        assert dot.startswith("digraph")
        assert "src/main.py::greet" in dot or "greet" in dot
        assert "defines" in dot or "DEFINES" in dot

    def test_to_dot_respects_max_nodes(self) -> None:
        from tyo3.graph.export import to_dot
        graph = CodeGraph()
        for i in range(5):
            node = SymbolNode(
                symbol_id=f"test.py::f{i}",
                name=f"f{i}",
                qualified_name=f"f{i}",
                kind=SymbolKind.FUNCTION,
                file="test.py",
                range=Range.model_validate(
                    {"start": {"line": i+1, "column": 1},
                     "end": {"line": i+1, "column": 1}}
                ),
            )
            graph._add_node(node)
            if i > 0:
                edge = EdgeData(kind=EdgeKind.REFERENCES)
                graph._add_edge(f"test.py::f{i-1}", f"test.py::f{i}",
                                edge, "test.py")
        dot = to_dot(graph, max_nodes=3)
        assert "0 [" in dot
        assert "2 [" in dot
        assert "4 [" not in dot

    def test_to_dot_empty_graph(self) -> None:
        from tyo3.graph.export import to_dot
        graph = CodeGraph()
        dot = to_dot(graph)
        assert dot.startswith("digraph")
        assert "}" in dot

    def test_to_dot_external_node_style(self) -> None:
        from tyo3.graph.export import to_dot
        graph = CodeGraph()
        ext_node = SymbolNode(
            symbol_id="stdlib::json.loads",
            name="loads",
            qualified_name="json.loads",
            kind=SymbolKind.FUNCTION,
            file="<external>",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1},
                 "end": {"line": 1, "column": 1}}
            ),
            external=True,
            package="stdlib",
        )
        graph._add_node(ext_node)
        dot = to_dot(graph)
        assert "style=dashed" in dot
        assert "[stdlib]" in dot or "stdlib" in dot

    def test_to_json_non_empty(self) -> None:
        from tyo3.graph.export import to_json
        graph = self._make_graph_with_data()
        data = to_json(graph)
        assert "nodes" in data
        assert "edges" in data
        assert len(data["nodes"]) == 2
        assert len(data["edges"]) == 1
        assert data["edges"][0]["data"]["kind"] == "defines"

    def test_to_json_empty_graph(self) -> None:
        from tyo3.graph.export import to_json
        graph = CodeGraph()
        data = to_json(graph)
        assert data == {"nodes": [], "edges": []}

    def test_to_json_edge_with_range(self) -> None:
        from tyo3.graph.export import to_json
        graph = CodeGraph()
        a = SymbolNode(
            symbol_id="a.py::foo",
            name="foo",
            qualified_name="foo",
            kind=SymbolKind.FUNCTION,
            file="a.py",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1},
                 "end": {"line": 1, "column": 1}}
            ),
        )
        b = SymbolNode(
            symbol_id="b.py::bar",
            name="bar",
            qualified_name="bar",
            kind=SymbolKind.FUNCTION,
            file="b.py",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1},
                 "end": {"line": 1, "column": 1}}
            ),
        )
        graph._add_node(a)
        graph._add_node(b)
        edge = EdgeData(
            kind=EdgeKind.REFERENCES,
            file="a.py",
            range=Range.model_validate(
                {"start": {"line": 5, "column": 10},
                 "end": {"line": 5, "column": 13}}
            ),
            role=ReferenceRole.READ,
        )
        graph._add_edge("a.py::foo", "b.py::bar", edge, "a.py")
        data = to_json(graph)
        edge_data = data["edges"][0]["data"]
        assert edge_data["kind"] == "references"
        assert edge_data["role"] == "read"
        assert edge_data["file"] == "a.py"
        assert edge_data["range"]["start"]["line"] == 5
