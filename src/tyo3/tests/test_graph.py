"""Tests for the CodeGraph."""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

from tyo3.graph import CodeGraph, EdgeData, EdgeKind, SymbolNode
from tyo3.graph.identity import make_symbol_id, symbol_id_from_symbol
from tyo3.graph.models import ReferenceRole
from tyo3.models.symbols import SymbolKind

try:
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


needs_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="Rust native extension not built"
)


# ── Unit tests (no native required) ──


class TestSymbolIdentity:
    def test_make_symbol_id(self) -> None:
        sid = make_symbol_id("src/models.py", "User")
        assert sid == "src/models.py::User"

    def test_make_symbol_id_nested(self) -> None:
        sid = make_symbol_id("src/models.py", "User.save")
        assert sid == "src/models.py::User.save"


class TestEdgeData:
    def test_frozen(self) -> None:
        edge = EdgeData(kind=EdgeKind.REFERENCES)
        with pytest.raises(AttributeError):
            edge.kind = EdgeKind.IMPORTS  # type: ignore[misc]


class TestSymbolNode:
    def test_frozen(self) -> None:
        node = SymbolNode(
            symbol_id="test::Foo",
            name="Foo",
            qualified_name="Foo",
            kind=SymbolKind.CLASS,
            file="test.py",
            range={"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}},
        )
        with pytest.raises(Exception):
            node.name = "Bar"  # type: ignore[misc]

    def test_optional_fields_default(self) -> None:
        node = SymbolNode(
            symbol_id="test::Foo",
            name="Foo",
            qualified_name="Foo",
            kind=SymbolKind.CLASS,
            file="test.py",
            range={"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}},
        )
        assert node.documentation is None
        assert node.signature is None
        assert node.external is False
        assert node.package is None


class TestReferenceRoleEnum:
    def test_all_roles_are_valid_str_enum(self) -> None:
        assert ReferenceRole.READ == "read"
        assert ReferenceRole.WRITE == "write"
        assert ReferenceRole.IMPORT == "import"
        assert ReferenceRole.DEFINITION == "definition"
        assert ReferenceRole.OTHER == "other"


class TestEdgeKindEnum:
    def test_all_kinds_are_valid_str_enum(self) -> None:
        assert EdgeKind.DEFINES == "defines"
        assert EdgeKind.CONTAINS == "contains"
        assert EdgeKind.REFERENCES == "references"
        assert EdgeKind.IMPORTS == "imports"
        assert EdgeKind.INHERITS == "inherits"
        assert EdgeKind.OVERRIDES == "overrides"
        assert EdgeKind.TYPE_OF == "type_of"
        assert EdgeKind.RETURNS == "returns"
        assert EdgeKind.INSTANTIATES == "instantiates"


# ── Integration tests (require native extension) ──


@needs_native
class TestGraphConstruction:
    def test_build_simple(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("simple_package")) as session:
            graph = CodeGraph.build(session)
            assert graph.node_count > 0
            assert graph.edge_count >= 0

    def test_nodes_have_module(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("simple_package")) as session:
            graph = CodeGraph.build(session)
            modules = graph.symbols_of_kind(SymbolKind.MODULE)
            assert len(modules) > 0

    def test_containment_edges_exist(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
            # Verify that the MODULE node has children (containment edges)
            modules = graph.symbols_of_kind(SymbolKind.MODULE)
            assert len(modules) > 0
            # At least one module should have children
            modules_with_kids = [m for m in modules if len(graph.children(m.symbol_id)) > 0]
            assert len(modules_with_kids) > 0, "At least one module should have children"

    def test_parent_finds_module(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
            modules = graph.symbols_of_kind(SymbolKind.MODULE)
            for m in modules:
                kids = graph.children(m.symbol_id)
                if kids:
                    # A child should find its way back to the parent module
                    parent = graph.parent(kids[0].symbol_id)
                    assert parent is not None
                    assert parent.kind == SymbolKind.MODULE
                    break

    def test_diagnostics_collection_works(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            all_diags = graph.all_diagnostics()
            assert isinstance(all_diags, list)
            # bug.py has an unsuppressed type error
            has_bug_diag = any("bug.py" in (d.file or "") for d in all_diags)
            assert has_bug_diag, f"Expected diagnostics for bug.py, got {len(all_diags)} total"


@needs_native
class TestGraphQueries:
    def test_symbol_lookup(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
            # Find a class by iterating all nodes
            classes = graph.symbols_of_kind(SymbolKind.CLASS)
            assert len(classes) > 0
            # Look it up by ID
            found = graph.symbol(classes[0].symbol_id)
            assert found is not None
            assert found.symbol_id == classes[0].symbol_id

    def test_symbols_in_file(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
            files = session.files()
            assert len(files) > 0
            symbols = graph.symbols_in_file(str(files[0]))
            assert len(symbols) > 0

    def test_children(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
            modules = graph.symbols_of_kind(SymbolKind.MODULE)
            if modules:
                kids = graph.children(modules[0].symbol_id)
                assert len(kids) > 0

    def test_transitive_dependencies_returns_set(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("imports")) as session:
            graph = CodeGraph.build(session)
            # Any symbol should have a non-None result (possibly empty set)
            for idx in graph.graph.node_indices():
                node = graph.graph[idx]
                deps = graph.transitive_dependencies(node.symbol_id)
                assert isinstance(deps, set)
                break
