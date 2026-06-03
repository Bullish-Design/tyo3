"""Tests for import cycle detection and strongly-connected components."""

from __future__ import annotations

from tyo3.graph import CodeGraph, EdgeData, EdgeKind, SymbolNode
from tyo3.models.analysis import Range
from tyo3.models.symbols import SymbolKind
from tyo3.tests.conftest import needs_native, get_graph


class TestImportCycles:
    """Unit tests for import cycle detection."""

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

    def _add_import_edge(
        self, graph: CodeGraph, src_file: str, tgt_file: str
    ) -> None:
        src_id = f"{src_file}::<module>"
        tgt_id = f"{tgt_file}::<module>"
        edge = EdgeData(kind=EdgeKind.IMPORTS)
        graph._add_edge(src_id, tgt_id, edge, src_file)

    def _make_data_node(
        self, graph: CodeGraph, symbol_id: str, file: str,
    ) -> None:
        node = SymbolNode(
            symbol_id=symbol_id,
            name=symbol_id.split("::")[-1],
            qualified_name=symbol_id.split("::")[-1],
            kind=SymbolKind.FUNCTION,
            file=file,
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1},
                 "end": {"line": 1, "column": 1}}
            ),
        )
        graph._add_node(node)

    def test_empty_graph_no_cycles(self) -> None:
        graph = CodeGraph()
        assert graph.import_cycles() == []

    def test_single_module_no_cycles(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        assert graph.import_cycles() == []

    def test_linear_no_cycles(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._make_module_node(graph, "b.py", "b")
        self._make_module_node(graph, "c.py", "c")
        self._add_import_edge(graph, "a.py", "b.py")
        self._add_import_edge(graph, "b.py", "c.py")
        assert graph.import_cycles() == []

    def test_simple_cycle(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._make_module_node(graph, "b.py", "b")
        self._add_import_edge(graph, "a.py", "b.py")
        self._add_import_edge(graph, "b.py", "a.py")
        cycles = graph.import_cycles()
        assert len(cycles) == 1
        cycle_ids = set(cycles[0])
        assert "a.py::<module>" in cycle_ids
        assert "b.py::<module>" in cycle_ids
        assert len(cycles[0]) == 2

    def test_three_node_cycle(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._make_module_node(graph, "b.py", "b")
        self._make_module_node(graph, "c.py", "c")
        self._add_import_edge(graph, "a.py", "b.py")
        self._add_import_edge(graph, "b.py", "c.py")
        self._add_import_edge(graph, "c.py", "a.py")
        cycles = graph.import_cycles()
        assert len(cycles) >= 1
        cycle_ids = set(cycles[0])
        assert "a.py::<module>" in cycle_ids
        assert "b.py::<module>" in cycle_ids
        assert "c.py::<module>" in cycle_ids

    def test_self_imports_ignored(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._add_import_edge(graph, "a.py", "a.py")
        assert graph.import_cycles() == []

    def test_dag_no_cycles(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._make_module_node(graph, "b.py", "b")
        self._make_module_node(graph, "c.py", "c")
        self._make_module_node(graph, "d.py", "d")
        self._add_import_edge(graph, "a.py", "b.py")
        self._add_import_edge(graph, "a.py", "c.py")
        self._add_import_edge(graph, "b.py", "d.py")
        self._add_import_edge(graph, "c.py", "d.py")
        assert graph.import_cycles() == []

    def test_references_edges_also_detected(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "x.py", "x")
        self._make_module_node(graph, "y.py", "y")
        src_id = "x.py::<module>"
        tgt_id = "y.py::<module>"
        edge_a = EdgeData(kind=EdgeKind.REFERENCES, file="x.py")
        edge_b = EdgeData(kind=EdgeKind.REFERENCES, file="y.py")
        graph._add_edge(src_id, tgt_id, edge_a, "x.py")
        graph._add_edge(tgt_id, src_id, edge_b, "y.py")
        cycles = graph.import_cycles()
        assert len(cycles) == 1


class TestImportCycleGroups:
    """Unit tests for import_cycle_groups (Phase E1)."""

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

    def _add_import_edge(
        self, graph: CodeGraph, src_file: str, tgt_file: str
    ) -> None:
        src_id = f"{src_file}::<module>"
        tgt_id = f"{tgt_file}::<module>"
        edge = EdgeData(kind=EdgeKind.IMPORTS)
        graph._add_edge(src_id, tgt_id, edge, src_file)

    def test_empty_graph(self) -> None:
        graph = CodeGraph()
        assert graph.import_cycle_groups() == []

    def test_single_module(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        assert graph.import_cycle_groups() == []

    def test_simple_cycle(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._make_module_node(graph, "b.py", "b")
        self._add_import_edge(graph, "a.py", "b.py")
        self._add_import_edge(graph, "b.py", "a.py")
        groups = graph.import_cycle_groups()
        assert len(groups) == 1
        assert len(groups[0]) == 2
        assert "a.py::<module>" in groups[0]
        assert "b.py::<module>" in groups[0]

    def test_three_node_cycle(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._make_module_node(graph, "b.py", "b")
        self._make_module_node(graph, "c.py", "c")
        self._add_import_edge(graph, "a.py", "b.py")
        self._add_import_edge(graph, "b.py", "c.py")
        self._add_import_edge(graph, "c.py", "a.py")
        groups = graph.import_cycle_groups()
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_two_separate_cycles(self) -> None:
        graph = CodeGraph()
        # Cycle 1: a <-> b
        self._make_module_node(graph, "a.py", "a")
        self._make_module_node(graph, "b.py", "b")
        self._add_import_edge(graph, "a.py", "b.py")
        self._add_import_edge(graph, "b.py", "a.py")
        # Cycle 2: c <-> d
        self._make_module_node(graph, "c.py", "c")
        self._make_module_node(graph, "d.py", "d")
        self._add_import_edge(graph, "c.py", "d.py")
        self._add_import_edge(graph, "d.py", "c.py")
        groups = graph.import_cycle_groups()
        assert len(groups) == 2
        all_modules = set()
        for g in groups:
            all_modules.update(g)
        assert len(all_modules) == 4

    def test_dag_no_groups(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._make_module_node(graph, "b.py", "b")
        self._make_module_node(graph, "c.py", "c")
        self._add_import_edge(graph, "a.py", "b.py")
        self._add_import_edge(graph, "b.py", "c.py")
        assert graph.import_cycle_groups() == []

    def test_self_import_ignored(self) -> None:
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._add_import_edge(graph, "a.py", "a.py")
        assert graph.import_cycle_groups() == []


@needs_native
class TestImportCyclesIntegration:
    """Integration tests for import cycles on real fixtures."""

    def test_no_false_cycles_on_simple_package(self) -> None:
        graph = get_graph("simple_package")
        cycles = graph.import_cycles()
        assert len(cycles) == 0, (
            f"simple_package should have no import cycles, got {cycles}"
        )

    def test_does_not_crash_on_complex_fixture(self) -> None:
        graph = get_graph("circular_imports")
        cycles = graph.import_cycles()
        assert isinstance(cycles, list)

    def test_graph_test_fixture_no_crash(self) -> None:
        graph = get_graph("graph_test")
        cycles = graph.import_cycles()
        assert isinstance(cycles, list)

    def test_cycle_groups_no_false_positives(self) -> None:
        graph = get_graph("simple_package")
        groups = graph.import_cycle_groups()
        assert groups == []

    def test_cycle_groups_returns_list(self) -> None:
        graph = get_graph("graph_test")
        groups = graph.import_cycle_groups()
        assert isinstance(groups, list)
