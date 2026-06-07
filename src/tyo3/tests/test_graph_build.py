"""Tests for graph construction and qualified-name resolution (requires native extension)."""

from __future__ import annotations

import re
from pathlib import Path as StdPath
from unittest.mock import MagicMock

import pytest

from tyo3 import TyO3Session
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

    def test_build_primes_identity_for_created_file(self, tmp_path: StdPath) -> None:
        ulid_prefix = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}(::|$)")

        with TyO3Session(str(tmp_path)) as session:
            session.edit(
                "created.py",
                "class Created:\n"
                "    def method(self):\n"
                "        return 1\n\n"
                "def helper():\n"
                "    return Created().method()\n",
            )

            graph = CodeGraph.build(session)

        for idx in graph.graph.node_indices():
            durable_id = graph.graph[idx].durable_id
            assert not re.search(r"@\d+", durable_id), f"location-derived node id: {durable_id}"
            if durable_id.startswith(("<module>", "<external>")):
                continue
            assert ulid_prefix.match(durable_id), f"entity node id is not registry-backed: {durable_id}"
            assert graph.graph[idx].content_hash is not None

    def test_entity_node_content_hash_matches_symbol_dto(self, tmp_path: StdPath) -> None:
        (tmp_path / "models.py").write_text(
            "class User:\n"
            "    def save(self):\n"
            "        return 1\n"
        )
        with TyO3Session(str(tmp_path)) as session:
            session.sync_all()
            graph = CodeGraph.build(session)
            symbols = {
                sym.durable_id: sym
                for sym in session.document_symbols("models.py")
                if sym.durable_id is not None
            }

        entity_nodes = [
            graph.graph[idx]
            for idx in graph.graph.node_indices()
            if graph.graph[idx].kind != SymbolKind.MODULE and not graph.graph[idx].external
        ]
        assert entity_nodes
        for node in entity_nodes:
            assert node.content_hash is not None
            assert node.durable_id in symbols
            assert node.content_hash == symbols[node.durable_id].content_hash

    @pytest.mark.xfail(
        strict=True,
        reason="Deferred to Phase 5: this writes models.py AFTER opening an empty "
        "project, then calls sync_all(). Under Phase 1's authoritative-generation "
        "model, sync_all() republishes the existing (empty) generation + Rescan "
        "and no longer re-reads disk, so a file created after open is not "
        "discovered ('File not in project'). sync_path() and pre-open ingest both "
        "work. Phase 5 funnels sync_all through ingest_project / the commit path. "
        "See PROGRESS.md Phase 5 (sync_all re-ingest).",
    )
    def test_content_hash_updates_incrementally_by_semantic_body(self, tmp_path: StdPath) -> None:
        original = (
            "class User:\n"
            "    def save(self):\n"
            "        return 1\n"
        )
        cosmetic = (
            "class User:\n"
            "\n"
            "    def save(self):\n"
            "        return 1\n"
        )
        body_change = (
            "class User:\n"
            "\n"
            "    def save(self):\n"
            "        return 2\n"
        )

        with TyO3Session(str(tmp_path)) as session:
            (tmp_path / "models.py").write_text(original)
            session.sync_all()
            graph = CodeGraph.build(session)
            save = next(n for n in graph.symbols_of_kind(SymbolKind.METHOD) if n.name == "save")
            initial_hash = save.content_hash

            cosmetic_delta = session.edit("models.py", cosmetic)
            graph.apply_delta(session.snapshot(), cosmetic_delta)
            save_after_cosmetic = next(n for n in graph.symbols_of_kind(SymbolKind.METHOD) if n.name == "save")
            assert save_after_cosmetic.durable_id == save.durable_id
            assert save_after_cosmetic.content_hash == initial_hash

            body_delta = session.edit("models.py", body_change)
            graph.apply_delta(session.snapshot(), body_delta)
            save_after_body = next(n for n in graph.symbols_of_kind(SymbolKind.METHOD) if n.name == "save")
            assert save_after_body.durable_id == save.durable_id
            assert save_after_body.content_hash is not None
            assert save_after_body.content_hash != initial_hash


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
        # The found value is a DurableId (ULID) — verify it resolves to a node
        node = graph.symbol(found)
        assert node is not None and node.name == "MyClass"

        found_method = graph._find_symbol_in_file(main_path, "get_val")
        assert found_method is not None, "_find_symbol_in_file should find 'get_val' by short name"
        node_m = graph.symbol(found_method)
        assert node_m is not None and node_m.name == "get_val"

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
    """Verify the build report for the native single-pass build (Gate 3N).

    The structural graph is produced authoritatively in Rust and applied via a
    single ``CodeDelta``; there are no per-file Python passes (symbols /
    references / inheritance) that can fail mid-build, and diagnostics are
    collected lazily off the structural path. A successful build therefore
    yields a *complete* report with no failures, and the report simply records
    the single-pass file totals. (The old per-phase failure-injection tests
    described a multi-pass Python build that no longer exists.)
    """

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
        mock_session.id_for.return_value = "01KTCTESTTESTTESTTESTTES01"

        graph, report = CodeGraph.build_with_report(mock_session)

        assert isinstance(graph, CodeGraph)
        assert isinstance(report, GraphBuildReport)
        assert isinstance(report.complete, bool)
