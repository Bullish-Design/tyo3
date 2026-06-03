"""Tests for graph construction and qualified-name resolution (requires native extension)."""

from __future__ import annotations

from pathlib import Path as StdPath

from tyo3.graph import SymbolNode
from tyo3.models.symbols import SymbolKind
from tyo3.tests.conftest import needs_native, get_graph, get_session

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


@needs_native
class TestGraphConstruction:
    def test_build_simple(self) -> None:
        graph = get_graph("simple_package")
        assert graph.node_count > 0
        assert graph.edge_count >= 0

    def test_nodes_have_module(self) -> None:
        graph = get_graph("simple_package")
        modules = graph.symbols_of_kind(SymbolKind.MODULE)
        assert len(modules) > 0

    def test_containment_edges_exist(self) -> None:
        graph = get_graph("classes")
        modules = graph.symbols_of_kind(SymbolKind.MODULE)
        assert len(modules) > 0
        modules_with_kids = [m for m in modules if len(graph.children(m.symbol_id)) > 0]
        assert len(modules_with_kids) > 0, "At least one module should have children"

    def test_parent_finds_module(self) -> None:
        graph = get_graph("classes")
        modules = graph.symbols_of_kind(SymbolKind.MODULE)
        for m in modules:
            kids = graph.children(m.symbol_id)
            if kids:
                parent = graph.parent(kids[0].symbol_id)
                assert parent is not None
                assert parent.kind == SymbolKind.MODULE
                break

    def test_diagnostics_collection_works(self) -> None:
        graph = get_graph("graph_test")
        all_diags = graph.all_diagnostics()
        assert isinstance(all_diags, list)
        has_bug_diag = any("bug.py" in (d.file or "") for d in all_diags)
        assert has_bug_diag, f"Expected diagnostics for bug.py, got {len(all_diags)} total"


@needs_native
class TestQualifiedNameResolution:
    """Verify REFERENCES edges connect to the correct nodes even when
    the batch occurrence API returns short names that differ from the
    qualified names used by document_symbols.
    """

    def test_nested_methods_have_correct_qualified_names(self) -> None:
        graph = get_graph("simple_package")
        methods = graph.symbols_of_kind(SymbolKind.METHOD)
        assert len(methods) >= 1, "Expected at least one method"
        for m in methods:
            if not m.external:
                assert "." in m.qualified_name, (
                    f"Method {m.name} should have dotted qualified_name, "
                    f"got {m.qualified_name!r}"
                )

    def test_find_symbol_in_file_resolves_by_short_name(self) -> None:
        graph = get_graph("simple_package")
        main_path = str(
            StdPath(fixture_path("simple_package")) / "main.py"
        )
        found = graph._find_symbol_in_file(main_path, "MyClass")
        assert found is not None, (
            "_find_symbol_in_file should find 'MyClass' by short name"
        )
        assert "MyClass" in found

        found_method = graph._find_symbol_in_file(main_path, "get_val")
        assert found_method is not None, (
            "_find_symbol_in_file should find 'get_val' by short name"
        )
        assert "get_val" in found_method

    def test_references_to_function_connect_correctly(self) -> None:
        graph = get_graph("simple_package")
        greet_nodes = [
            n for n in graph.symbols_of_kind(SymbolKind.FUNCTION)
            if n.name == "greet" and not n.external
        ]
        assert len(greet_nodes) >= 1, "greet function should exist"
        greet_node = greet_nodes[0]

        refs = graph.references_to(greet_node.symbol_id)
        assert len(refs) > 0, (
            f"greet() should have at least one reference, got {len(refs)}"
        )
        for r in refs:
            from tyo3.graph import EdgeKind as _EK
            assert r.kind == _EK.REFERENCES
            assert r.role is not None

    def test_occurrence_model_has_target_qualified_name(self) -> None:
        session = get_session("simple_package")
        main_path = str(
            StdPath(fixture_path("simple_package")) / "main.py"
        )
        occurrences = session.file_occurrences(main_path)  # type: ignore[union-attr]
        assert len(occurrences) > 0

        for occ in occurrences:
            assert hasattr(occ, "target_qualified_name"), (
                "NameOccurrence must have target_qualified_name"
            )

        has_qualified = any(
            occ.target_qualified_name is not None for occ in occurrences
        )
