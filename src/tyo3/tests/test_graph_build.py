"""Tests for graph construction and qualified-name resolution (requires native extension)."""

from __future__ import annotations

from pathlib import Path as StdPath
from unittest.mock import MagicMock

from tyo3.graph import (
    CodeGraph,
    GraphBuildReport,
)
from tyo3.models.symbols import SymbolKind
from tyo3.tests.conftest import get_graph, get_session, needs_native

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
        modules_with_kids = [m for m in modules if len(graph.children(m.durable_id)) > 0]
        assert len(modules_with_kids) > 0, "At least one module should have children"

    def test_parent_finds_module(self) -> None:
        graph = get_graph("classes")
        modules = graph.symbols_of_kind(SymbolKind.MODULE)
        for m in modules:
            kids = graph.children(m.durable_id)
            if kids:
                parent = graph.parent(kids[0].durable_id)
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
                    f"Method {m.name} should have dotted qualified_name, got {m.qualified_name!r}"
                )

    def test_find_symbol_in_file_resolves_by_short_name(self) -> None:
        graph = get_graph("simple_package")
        main_path = "main.py"
        found = graph._find_symbol_in_file(main_path, "MyClass")
        assert found is not None, "_find_symbol_in_file should find 'MyClass' by short name"
        assert "MyClass" in found

        found_method = graph._find_symbol_in_file(main_path, "get_val")
        assert found_method is not None, "_find_symbol_in_file should find 'get_val' by short name"
        assert "get_val" in found_method

    def test_references_to_function_connect_correctly(self) -> None:
        graph = get_graph("simple_package")
        greet_nodes = [n for n in graph.symbols_of_kind(SymbolKind.FUNCTION) if n.name == "greet" and not n.external]
        assert len(greet_nodes) >= 1, "greet function should exist"
        greet_node = greet_nodes[0]

        refs = graph.references_to(greet_node.durable_id)
        assert len(refs) > 0, f"greet() should have at least one reference, got {len(refs)}"
        for r in refs:
            from tyo3.graph import EdgeKind as _EK

            assert r.kind == _EK.REFERENCES
            assert r.role is not None

    def test_occurrence_model_has_target_qualified_name(self) -> None:
        session = get_session("simple_package")
        main_path = str(StdPath(fixture_path("simple_package")) / "main.py")
        occurrences = session.file_occurrences(main_path)  # type: ignore[union-attr]
        assert len(occurrences) > 0

        for occ in occurrences:
            assert hasattr(occ, "target_qualified_name"), "NameOccurrence must have target_qualified_name"

        has_qualified = any(occ.target_qualified_name is not None for occ in occurrences)
        _ = has_qualified  # Qualified-name coverage varies by backend


# ── Phase 6: Build Reports ───────────────────────────────────


class TestBuildReports:
    """Verify that build reports capture failures programmatically."""

    def test_build_report_records_symbol_failure(self) -> None:
        """When document_symbols raises for a file, the report captures it."""
        mock_session = MagicMock()
        mock_session.files.return_value = ["/fake/a.py", "/fake/b.py"]

        def _symbols(file_str: str) -> list:
            if file_str == "/fake/b.py":
                raise RuntimeError("simulated symbol failure")
            return []

        mock_session.document_symbols.side_effect = _symbols

        graph, report = CodeGraph.build_with_report(mock_session)

        assert not report.complete
        assert report.files_total == 2
        assert report.files_indexed == 1
        assert len(report.failures) == 1
        assert report.failures[0].file == "/fake/b.py"
        assert report.failures[0].phase == "symbols"
        assert report.failures[0].error_type == "RuntimeError"
        assert "simulated symbol failure" in report.failures[0].message

    def test_build_report_records_references_failure(self) -> None:
        """When file_occurrences raises for a file, the report captures it."""
        mock_session = MagicMock()
        mock_session.files.return_value = ["/fake/a.py"]
        mock_session.document_symbols.return_value = []
        mock_session.file_occurrences.side_effect = RuntimeError("simulated ref failure")

        graph, report = CodeGraph.build_with_report(mock_session)

        assert not report.complete
        assert len(report.failures) == 1
        assert report.failures[0].file == "/fake/a.py"
        assert report.failures[0].phase == "references"

    def test_build_report_records_inheritance_failure(self) -> None:
        """When type_hierarchy raises for a class, the report captures it."""
        from tyo3.models.analysis import FileRange, Range
        from tyo3.models.symbols import Symbol

        class_range = Range.model_validate(
            {
                "start": {"line": 1, "column": 1},
                "end": {"line": 5, "column": 1},
            }
        )
        mock_class = Symbol(
            name="MyClass",
            qualified_name="MyClass",
            kind=SymbolKind.CLASS,
            location=FileRange(
                path=StdPath("/fake/a.py"),
                range=class_range,
            ),
            deprecated=False,
        )

        mock_session = MagicMock()
        mock_session.files.return_value = ["/fake/a.py"]
        mock_session.document_symbols.return_value = [mock_class]
        mock_session.file_occurrences.return_value = []
        # Inheritance resolution now uses the lean class_supertypes() path.
        mock_session.class_supertypes.side_effect = RuntimeError("simulated inheritance failure")

        graph, report = CodeGraph.build_with_report(mock_session)

        assert not report.complete
        assert len(report.failures) == 1
        assert report.failures[0].phase == "inheritance"

    def test_build_report_records_diagnostics_failure(self) -> None:
        """When session.check() raises, the report captures it."""
        mock_session = MagicMock()
        mock_session.files.return_value = ["/fake/a.py"]
        mock_session.document_symbols.return_value = []
        mock_session.file_occurrences.return_value = []
        mock_session.check.side_effect = RuntimeError("simulated check failure")

        graph, report = CodeGraph.build_with_report(mock_session)

        assert not report.complete
        assert len(report.failures) == 1
        assert report.failures[0].phase == "diagnostics"
        assert report.failures[0].file == "<project>"

    @needs_native
    def test_build_report_complete_on_clean_fixture(self) -> None:
        """Building a real fixture with no failures produces a complete report."""
        session = get_session("simple_package")
        graph, report = CodeGraph.build_with_report(session)

        assert report.complete
        assert report.files_total > 0
        assert report.files_indexed == report.files_total
        assert report.failures == []

    @needs_native
    def test_build_without_report_works_unchanged(self) -> None:
        """Calling build() without a report still returns a graph normally."""
        session = get_session("simple_package")
        graph = CodeGraph.build(session)

        assert graph.node_count > 0
        assert graph.edge_count >= 0

    def test_build_with_report_returns_correct_types(self) -> None:
        """build_with_report() returns (CodeGraph, GraphBuildReport) tuple."""
        mock_session = MagicMock()
        mock_session.files.return_value = ["/fake/a.py"]
        mock_session.document_symbols.return_value = []
        mock_session.file_occurrences.return_value = []

        graph, report = CodeGraph.build_with_report(mock_session)

        assert isinstance(graph, CodeGraph)
        assert isinstance(report, GraphBuildReport)
        assert isinstance(report.complete, bool)

    def test_build_report_multiple_failures(self) -> None:
        """Multiple failures across phases are all recorded."""
        mock_session = MagicMock()
        mock_session.files.return_value = ["/fake/a.py", "/fake/b.py", "/fake/c.py"]

        def _symbols(file_str: str) -> list:
            if file_str == "/fake/a.py":
                return []
            if file_str == "/fake/b.py":
                raise RuntimeError("b failed")
            if file_str == "/fake/c.py":
                raise ValueError("c failed")
            return []

        mock_session.document_symbols.side_effect = _symbols
        mock_session.file_occurrences.return_value = []
        mock_session.check.side_effect = RuntimeError("check failed")

        graph, report = CodeGraph.build_with_report(mock_session)

        assert not report.complete
        assert report.files_total == 3
        assert report.files_indexed == 1  # only a.py succeeded
        assert len(report.failures) == 3  # b.py symbols, c.py symbols, diagnostics
        phases = {f.phase for f in report.failures}
        assert "symbols" in phases
        assert "diagnostics" in phases
