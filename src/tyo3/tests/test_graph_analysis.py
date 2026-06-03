"""Tests for hub symbols, subgraph extraction, and graph property queries."""

from __future__ import annotations

import pytest
import rustworkx as rx

from tyo3.graph import CodeGraph, EdgeData, EdgeKind, SymbolNode
from tyo3.models.analysis import Range
from tyo3.models.symbols import SymbolKind
from pathlib import Path as StdPath
from tyo3.tests.conftest import needs_native, get_graph


class TestHubSymbols:
    def _make_node(
        self, graph: CodeGraph, name: str, file: str = "test.py"
    ) -> str:
        sid = f"{file}::{name}"
        node = SymbolNode(
            symbol_id=sid,
            name=name,
            qualified_name=name,
            kind=SymbolKind.FUNCTION,
            file=file,
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1},
                 "end": {"line": 1, "column": 1}}
            ),
        )
        graph._add_node(node)
        return sid

    def _link(
        self, graph: CodeGraph, a: str, b: str, file: str = "test.py"
    ) -> None:
        edge = EdgeData(kind=EdgeKind.REFERENCES)
        graph._add_edge(a, b, edge, file)

    def test_empty_graph(self) -> None:
        graph = CodeGraph()
        assert graph.hub_symbols() == []

    def test_single_node(self) -> None:
        graph = CodeGraph()
        self._make_node(graph, "foo")
        hubs = graph.hub_symbols(top_n=5)
        assert len(hubs) == 0

    def test_star_topology(self) -> None:
        graph = CodeGraph()
        center = self._make_node(graph, "center")
        leaves = ["leaf_a", "leaf_b", "leaf_c", "leaf_d", "leaf_e"]
        for leaf in leaves:
            sid = self._make_node(graph, leaf)
            self._link(graph, center, sid)
            self._link(graph, sid, center)
        hubs = graph.hub_symbols(top_n=3)
        assert len(hubs) > 0
        if hubs:
            top_id, top_score = hubs[0]
            assert top_id == center, f"Expected {center} as top hub, got {top_id}"

    def test_respects_top_n(self) -> None:
        graph = CodeGraph()
        for i in range(10):
            self._make_node(graph, f"n{i}")
        for i in range(9):
            a = f"test.py::n{i}"
            b = f"test.py::n{i+1}"
            self._link(graph, a, b)
        hubs = graph.hub_symbols(top_n=3)
        assert len(hubs) <= 3


class TestSubgraphForFile:
    def _make_module_node(
        self, graph: CodeGraph, file: str, name: str
    ) -> str:
        sid = f"{file}::<module>"
        node = SymbolNode(
            symbol_id=sid,
            name=name,
            qualified_name="<module>",
            kind=SymbolKind.MODULE,
            file=file,
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1},
                 "end": {"line": 1, "column": 1}}
            ),
        )
        graph._add_node(node)
        return sid

    def _make_func_node(
        self, graph: CodeGraph, name: str, file: str
    ) -> str:
        sid = f"{file}::{name}"
        node = SymbolNode(
            symbol_id=sid,
            name=name,
            qualified_name=name,
            kind=SymbolKind.FUNCTION,
            file=file,
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1},
                 "end": {"line": 1, "column": 1}}
            ),
        )
        graph._add_node(node)
        return sid

    def test_empty_file_returns_empty_graph(self) -> None:
        graph = CodeGraph()
        sub = graph.subgraph_for_file("nonexistent.py")
        assert sub.num_nodes() == 0

    def test_extracts_file_nodes_and_neighbours(self) -> None:
        graph = CodeGraph()
        mod_a = self._make_module_node(graph, "a.py", "a")
        mod_b = self._make_module_node(graph, "b.py", "b")
        func_a = self._make_func_node(graph, "foo", "a.py")
        func_b = self._make_func_node(graph, "bar", "b.py")

        edge = EdgeData(kind=EdgeKind.REFERENCES)
        graph._add_edge(func_a, func_b, edge, "a.py")

        sub = graph.subgraph_for_file("a.py")
        assert sub.num_nodes() >= 2
        sub_sids = {sub[i].symbol_id for i in sub.node_indices()}
        assert mod_a in sub_sids
        assert func_a in sub_sids
        assert func_b in sub_sids

    def test_preserves_edges(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        func_a = self._make_func_node(graph, "foo", "a.py")
        func_b = self._make_func_node(graph, "bar", "a.py")
        edge = EdgeData(kind=EdgeKind.CONTAINS)
        graph._add_edge("a.py::<module>", func_a, edge, "a.py")

        sub = graph.subgraph_for_file("a.py")
        assert sub.num_edges() >= 1


# ── Phase E: Graph property queries ──


class TestGraphProperties:
    """Tests for has_cycles, is_reachable, topological_order (Phase E2-E4)."""

    def _make_node(
        self, graph: CodeGraph, name: str, file: str = "test.py",
        kind: SymbolKind = SymbolKind.FUNCTION,
    ) -> str:
        sid = f"{file}::{name}"
        node = SymbolNode(
            symbol_id=sid,
            name=name,
            qualified_name=name,
            kind=kind,
            file=file,
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1},
                 "end": {"line": 1, "column": 1}}
            ),
        )
        graph._add_node(node)
        return sid

    def _link(
        self, graph: CodeGraph, a: str, b: str, file: str = "test.py"
    ) -> None:
        edge = EdgeData(kind=EdgeKind.REFERENCES)
        graph._add_edge(a, b, edge, file)

    # ── has_cycles ──

    def test_has_cycles_empty_graph(self) -> None:
        graph = CodeGraph()
        assert graph.has_cycles is False

    def test_has_cycles_single_node(self) -> None:
        graph = CodeGraph()
        self._make_node(graph, "a")
        assert graph.has_cycles is False

    def test_has_cycles_dag(self) -> None:
        graph = CodeGraph()
        self._make_node(graph, "a")
        self._make_node(graph, "b")
        self._make_node(graph, "c")
        self._link(graph, "test.py::a", "b")
        self._link(graph, "test.py::b", "c")
        assert graph.has_cycles is False

    def test_has_cycles_with_cycle(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        b = self._make_node(graph, "b")
        self._link(graph, a, b)
        self._link(graph, b, a)
        assert graph.has_cycles is True

    def test_has_cycles_three_node_cycle(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        b = self._make_node(graph, "b")
        c = self._make_node(graph, "c")
        self._link(graph, a, b)
        self._link(graph, b, c)
        self._link(graph, c, a)
        assert graph.has_cycles is True

    # ── is_reachable ──

    def test_is_reachable_direct_edge(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        b = self._make_node(graph, "b")
        self._link(graph, a, b)
        assert graph.is_reachable(a, b) is True

    def test_is_reachable_transitive(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        b = self._make_node(graph, "b")
        c = self._make_node(graph, "c")
        self._link(graph, a, b)
        self._link(graph, b, c)
        assert graph.is_reachable(a, c) is True

    def test_is_reachable_not_reachable(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        b = self._make_node(graph, "b")
        self._link(graph, a, b)
        assert graph.is_reachable(b, a) is False

    def test_is_reachable_same_node(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        # has_path with same src/tgt returns False without a self-loop
        assert graph.is_reachable(a, a) is False

    def test_is_reachable_unknown_source(self) -> None:
        graph = CodeGraph()
        b = self._make_node(graph, "b")
        assert graph.is_reachable("nonexistent::a", b) is False

    def test_is_reachable_unknown_target(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        assert graph.is_reachable(a, "nonexistent::b") is False

    def test_is_reachable_disjoint(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        b = self._make_node(graph, "b")
        assert graph.is_reachable(a, b) is False

    # ── topological_order ──

    def test_topological_order_empty(self) -> None:
        graph = CodeGraph()
        order = graph.topological_order()
        assert order == []

    def test_topological_order_single(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        order = graph.topological_order()
        assert len(order) == 1
        assert order[0] == a

    def test_topological_order_linear(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        b = self._make_node(graph, "b")
        c = self._make_node(graph, "c")
        self._link(graph, a, b)
        self._link(graph, b, c)
        order = graph.topological_order()
        assert len(order) == 3
        assert order.index(a) < order.index(b)
        assert order.index(b) < order.index(c)

    def test_topological_order_diamond(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        b = self._make_node(graph, "b")
        c = self._make_node(graph, "c")
        d = self._make_node(graph, "d")
        self._link(graph, a, b)
        self._link(graph, a, c)
        self._link(graph, b, d)
        self._link(graph, c, d)
        order = graph.topological_order()
        assert len(order) == 4
        assert order.index(a) < order.index(b)
        assert order.index(a) < order.index(c)
        assert order.index(b) < order.index(d)
        assert order.index(c) < order.index(d)

    def test_topological_order_with_cycle_returns_empty(self) -> None:
        graph = CodeGraph()
        a = self._make_node(graph, "a")
        b = self._make_node(graph, "b")
        self._link(graph, a, b)
        self._link(graph, b, a)
        order = graph.topological_order()
        assert order == []


@needs_native
class TestHubSymbolsIntegration:
    """Integration tests for hub symbol detection."""

    def test_hub_symbols_returns_list(self) -> None:
        graph = get_graph("graph_test")
        hubs = graph.hub_symbols(top_n=5)
        assert isinstance(hubs, list)
        for sid, score in hubs:
            assert isinstance(sid, str)
            assert isinstance(score, float)
            assert score > 0.0

    def test_hub_symbols_sorted_descending(self) -> None:
        graph = get_graph("classes")
        hubs = graph.hub_symbols(top_n=10)
        if len(hubs) >= 2:
            for i in range(len(hubs) - 1):
                assert hubs[i][1] >= hubs[i+1][1], (
                    f"Hubs not sorted: {hubs[i][1]} < {hubs[i+1][1]}"
                )


@needs_native
class TestSubgraphForFileIntegration:
    """Integration tests for file subgraph extraction."""

    def test_extracts_meaningful_subgraph(self) -> None:
        graph = get_graph("graph_test")
        fixture = StdPath(__file__).parent.parent.parent.parent / "fixtures"
        models_path = str(fixture / "graph_test" / "models.py")
        sub = graph.subgraph_for_file(models_path)
        assert sub.num_nodes() > 0
        node_names = {sub[i].name for i in sub.node_indices()}
        assert len(node_names) >= 1

    def test_export_dot_of_subgraph(self) -> None:
        graph = get_graph("graph_test")
        fixture = StdPath(__file__).parent.parent.parent.parent / "fixtures"
        models_path = str(fixture / "graph_test" / "models.py")
        sub = graph.subgraph_for_file(models_path)
        assert sub.num_nodes() > 0
        for idx in sub.node_indices():
            node = sub[idx]
            assert isinstance(node, SymbolNode)
            break


@needs_native
class TestGraphPropertiesIntegration:
    """Integration tests for Phase E graph property queries on real fixtures."""

    def test_has_cycles_on_graph_test(self) -> None:
        graph = get_graph("graph_test")
        # Just verify it returns a bool and doesn't crash
        result = graph.has_cycles
        assert isinstance(result, bool)

    def test_has_cycles_on_simple_package(self) -> None:
        graph = get_graph("simple_package")
        result = graph.has_cycles
        assert isinstance(result, bool)

    def test_topological_order_on_graph_test(self) -> None:
        graph = get_graph("graph_test")
        order = graph.topological_order()
        # If there are cycles, topological_order returns []; otherwise a list of SIDs
        assert isinstance(order, list)

    def test_is_reachable_within_same_file(self) -> None:
        graph = get_graph("graph_test")
        # Find a module node and check reachability
        mod_sid = None
        for idx in graph.graph.node_indices():
            node = graph.graph[idx]
            if node.kind == SymbolKind.MODULE and not node.external:
                mod_sid = node.symbol_id
                # Find its children
                children = graph.children(mod_sid)
                if children:
                    # Module should be reachable to its children via containment edges
                    child_sid = children[0].symbol_id
                    reachable = graph.is_reachable(mod_sid, child_sid)
                    # Module DEFINES its children, so this is a direct edge
                    assert reachable is True
                    break
        # Should have found and tested at least one reachable pair
        assert mod_sid is not None
