"""Tests for graph queries — symbol lookups, children, dependencies (requires native extension)."""

from __future__ import annotations

from tyo3.graph import CodeGraph, EdgeData, EdgeKind, SymbolNode
from tyo3.graph.models import ReferenceRole
from tyo3.models.analysis import Range
from tyo3.models.symbols import SymbolKind
from tyo3.tests.conftest import needs_native, get_graph, get_session


@needs_native
class TestGraphQueries:
    def test_symbol_lookup(self) -> None:
        graph = get_graph("classes")
        classes = graph.symbols_of_kind(SymbolKind.CLASS)
        assert len(classes) > 0
        found = graph.symbol(classes[0].symbol_id)
        assert found is not None
        assert found.symbol_id == classes[0].symbol_id

    def test_symbols_in_file(self) -> None:
        graph = get_graph("classes")
        session = get_session("classes")
        files = session.files()  # type: ignore[union-attr]
        assert len(files) > 0
        symbols = graph.symbols_in_file(str(files[0]))
        assert len(symbols) > 0

    def test_children(self) -> None:
        graph = get_graph("classes")
        modules = graph.symbols_of_kind(SymbolKind.MODULE)
        if modules:
            kids = graph.children(modules[0].symbol_id)
            assert len(kids) > 0

    def test_transitive_dependencies_returns_set(self) -> None:
        graph = get_graph("imports")
        for idx in graph.graph.node_indices():
            node: SymbolNode = graph.graph[idx]
            deps = graph.transitive_dependencies(node.symbol_id)
            assert isinstance(deps, set)
            break

    def test_external_symbols_is_list(self) -> None:
        graph = get_graph("graph_test")
        result = graph.external_symbols()
        assert isinstance(result, list)
        for node in result:
            assert node.external is True

    def test_external_symbols_by_package_is_dict(self) -> None:
        graph = get_graph("graph_test")
        result = graph.external_symbols_by_package()
        assert isinstance(result, dict)

    def test_inheritance_for_graph_test_classes(self) -> None:
        graph = get_graph("graph_test")
        all_classes = graph.symbols_of_kind(SymbolKind.CLASS)
        local_classes = [c for c in all_classes if not c.external]
        assert len(local_classes) >= 2, f"Expected at least Base + User class, got {len(local_classes)}"
        for cls in local_classes:
            parent = graph.parent(cls.symbol_id)
            assert parent is not None, f"Class {cls.name} has no parent"


class TestParallelEdges:
    """Verify that multiple edges between the same node pair are handled."""

    def test_references_to_returns_all_parallel_edges(self) -> None:
        graph = CodeGraph()
        a = SymbolNode(
            symbol_id="a.py::caller",
            name="caller", qualified_name="caller",
            kind=SymbolKind.FUNCTION, file="a.py",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}}
            ),
        )
        b = SymbolNode(
            symbol_id="b.py::target",
            name="target", qualified_name="target",
            kind=SymbolKind.FUNCTION, file="b.py",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 5, "column": 1}}
            ),
        )
        graph._add_node(a)
        graph._add_node(b)

        for line in [3, 5, 7]:
            edge = EdgeData(
                kind=EdgeKind.REFERENCES,
                file="a.py",
                range=Range.model_validate(
                    {"start": {"line": line, "column": 5},
                     "end": {"line": line, "column": 11}}
                ),
                role=ReferenceRole.READ,
            )
            graph._add_edge("a.py::caller", "b.py::target", edge, "a.py")

        refs = graph.references_to("b.py::target")
        assert len(refs) == 3, f"Expected 3 references, got {len(refs)}"

        from_refs = graph.references_from("a.py::caller")
        assert len(from_refs) == 3

    def test_children_dedup_with_parallel_containment_edges(self) -> None:
        graph = CodeGraph()
        mod = SymbolNode(
            symbol_id="m.py::<module>",
            name="m", qualified_name="<module>",
            kind=SymbolKind.MODULE, file="m.py",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
            ),
        )
        func = SymbolNode(
            symbol_id="m.py::foo",
            name="foo", qualified_name="foo",
            kind=SymbolKind.FUNCTION, file="m.py",
            range=Range.model_validate(
                {"start": {"line": 2, "column": 1}, "end": {"line": 5, "column": 1}}
            ),
        )
        graph._add_node(mod)
        graph._add_node(func)

        e1 = EdgeData(kind=EdgeKind.DEFINES, file="m.py")
        e2 = EdgeData(kind=EdgeKind.DEFINES, file="m.py")
        graph._add_edge("m.py::<module>", "m.py::foo", e1, "m.py")
        graph._add_edge("m.py::<module>", "m.py::foo", e2, "m.py")

        kids = graph.children("m.py::<module>")
        assert len(kids) == 1, f"Expected 1 child (dedup), got {len(kids)}"
        assert kids[0].name == "foo"

    def test_references_to_unknown_symbol_returns_empty(self) -> None:
        graph = CodeGraph()
        refs = graph.references_to("nonexistent::symbol")
        assert refs == []

    def test_references_from_unknown_symbol_returns_empty(self) -> None:
        graph = CodeGraph()
        refs = graph.references_from("nonexistent::symbol")
        assert refs == []

    def test_children_unknown_symbol_returns_empty(self) -> None:
        graph = CodeGraph()
        kids = graph.children("nonexistent::symbol")
        assert kids == []

    def test_parent_unknown_symbol_returns_none(self) -> None:
        graph = CodeGraph()
        p = graph.parent("nonexistent::symbol")
        assert p is None
