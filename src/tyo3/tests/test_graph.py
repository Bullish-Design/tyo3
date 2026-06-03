"""Tests for the CodeGraph."""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

from tyo3.graph import CodeGraph, DependencyGraph, EdgeData, EdgeKind, SymbolNode
from tyo3.graph.identity import make_symbol_id, symbol_id_from_symbol
from tyo3.graph.models import ReferenceRole
from tyo3.models.analysis import Range
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

    def test_external_symbols_is_list(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            # external_symbols should always return a list (possibly empty)
            result = graph.external_symbols()
            assert isinstance(result, list)
            for node in result:
                assert node.external is True

    def test_external_symbols_by_package_is_dict(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            result = graph.external_symbols_by_package()
            assert isinstance(result, dict)

    def test_inheritance_for_graph_test_classes(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            all_classes = graph.symbols_of_kind(SymbolKind.CLASS)
            # Only check project-local classes (not external stub "object" etc.)
            local_classes = [c for c in all_classes if not c.external]
            assert len(local_classes) >= 2, f"Expected at least Base + User class, got {len(local_classes)}"
            for cls in local_classes:
                parent = graph.parent(cls.symbol_id)
                assert parent is not None, f"Class {cls.name} has no parent"


# ── DependencyGraph unit tests (no native required) ──


class TestDependencyGraph:
    def test_construction_and_lookup(self) -> None:
        import rustworkx as rx

        g = rx.PyDiGraph()
        node = SymbolNode(
            symbol_id="stdlib::json.loads",
            name="loads",
            qualified_name="json.loads",
            kind=SymbolKind.FUNCTION,
            file="<external>",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
            ),
            external=True,
            package="stdlib",
        )
        idx = g.add_node(node)
        id_to_index = {"stdlib::json.loads": idx}

        dep = DependencyGraph("stdlib", "unknown", g, id_to_index)
        assert dep.package == "stdlib"
        assert dep.version == "unknown"

        found = dep.lookup("stdlib::json.loads")
        assert found is not None
        assert found.name == "loads"
        assert found.external is True

        not_found = dep.lookup("stdlib::nonexistent")
        assert not_found is None

    def test_all_symbols(self) -> None:
        import rustworkx as rx

        g = rx.PyDiGraph()
        n1 = SymbolNode(
            symbol_id="pkg::A",
            name="A",
            qualified_name="A",
            kind=SymbolKind.CLASS,
            file="<external>",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
            ),
            external=True,
            package="pkg",
        )
        n2 = SymbolNode(
            symbol_id="pkg::B",
            name="B",
            qualified_name="B",
            kind=SymbolKind.CLASS,
            file="<external>",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
            ),
            external=True,
            package="pkg",
        )
        i1 = g.add_node(n1)
        i2 = g.add_node(n2)
        id_to_index = {"pkg::A": i1, "pkg::B": i2}

        dep = DependencyGraph("pkg", "1.0", g, id_to_index)
        all_syms = dep.all_symbols()
        assert len(all_syms) == 2
        names = {s.name for s in all_syms}
        assert names == {"A", "B"}

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch) -> None:
        import rustworkx as rx

        # Override CACHE_DIR to use tmp_path
        monkeypatch.setattr(
            "tyo3.graph.dependency.CACHE_DIR", tmp_path / "tyo3_deps"
        )

        g = rx.PyDiGraph()
        node = SymbolNode(
            symbol_id="testpkg::Foo",
            name="Foo",
            qualified_name="Foo",
            kind=SymbolKind.CLASS,
            file="<external>",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
            ),
            external=True,
            package="testpkg",
        )
        idx = g.add_node(node)
        id_to_index = {"testpkg::Foo": idx}

        dep = DependencyGraph("testpkg", "2.0", g, id_to_index)
        saved_path = dep.save()
        assert saved_path.exists()

        # Load it back
        loaded = DependencyGraph.load("testpkg", "2.0")
        assert loaded is not None
        assert loaded.package == "testpkg"
        assert loaded.version == "2.0"

        found = loaded.lookup("testpkg::Foo")
        assert found is not None
        assert found.name == "Foo"
        assert found.external is True
        assert found.package == "testpkg"

    def test_load_nonexistent_returns_none(self) -> None:
        result = DependencyGraph.load("nonexistent_pkg", "99.9")
        assert result is None

    def test_list_cached(self, tmp_path, monkeypatch) -> None:
        import rustworkx as rx

        monkeypatch.setattr(
            "tyo3.graph.dependency.CACHE_DIR", tmp_path / "tyo3_deps"
        )

        # Save two deps
        for pkg, ver in [("alpha", "1.0"), ("beta", "2.0")]:
            g = rx.PyDiGraph()
            node = SymbolNode(
                symbol_id=f"{pkg}::X",
                name="X",
                qualified_name="X",
                kind=SymbolKind.CLASS,
                file="<external>",
                range=Range.model_validate(
                    {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
                ),
                external=True,
                package=pkg,
            )
            idx = g.add_node(node)
            id_to_index = {f"{pkg}::X": idx}
            dep = DependencyGraph(pkg, ver, g, id_to_index)
            dep.save()

        cached = DependencyGraph.list_cached()
        assert len(cached) == 2
        cached_set = set(cached)
        assert ("alpha", "1.0") in cached_set
        assert ("beta", "2.0") in cached_set

    def test_list_cached_empty_directory(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "tyo3.graph.dependency.CACHE_DIR", tmp_path / "nonexistent_dir"
        )
        result = DependencyGraph.list_cached()
        assert result == []


class TestResolveExternal:
    def test_returns_same_node_when_not_external(self) -> None:
        """resolve_external returns the node unchanged for non-external symbols."""
        graph = CodeGraph()
        node = SymbolNode(
            symbol_id="src/main.py::foo",
            name="foo",
            qualified_name="foo",
            kind=SymbolKind.FUNCTION,
            file="src/main.py",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
            ),
            external=False,
        )
        graph._add_node(node)
        result = graph.resolve_external("src/main.py::foo")
        assert result is not None
        assert result.external is False

    def test_returns_stub_when_no_cache(self) -> None:
        """resolve_external returns the stub node when no cache exists."""
        graph = CodeGraph()
        graph._add_stub_node(
            symbol_id="unknown::Bar",
            name="Bar",
            qualified_name="Bar",
            kind=SymbolKind.CLASS,
            package="unknown",
        )
        result = graph.resolve_external("unknown::Bar")
        assert result is not None
        assert result.external is True
        assert result.package == "unknown"

    def test_resolves_from_cache(self, tmp_path, monkeypatch) -> None:
        """resolve_external loads from cached dependency graph."""
        import rustworkx as rx

        monkeypatch.setattr(
            "tyo3.graph.dependency.CACHE_DIR", tmp_path / "tyo3_deps"
        )

        # Save a cached dep graph with a fully-resolved node
        g = rx.PyDiGraph()
        rich_node = SymbolNode(
            symbol_id="stdlib::pathlib.Path",
            name="Path",
            qualified_name="pathlib.Path",
            kind=SymbolKind.CLASS,
            file="<external>",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
            ),
            external=True,
            package="stdlib",
            documentation="Represents a filesystem path.",
            signature="class Path(*args, **kwargs)",
        )
        idx = g.add_node(rich_node)
        id_to_index = {"stdlib::pathlib.Path": idx}
        dep = DependencyGraph("stdlib", "unknown", g, id_to_index)
        dep.save()

        # Create a CodeGraph with only a stub node
        graph = CodeGraph()
        graph._add_stub_node(
            symbol_id="stdlib::pathlib.Path",
            name="Path",
            qualified_name="pathlib.Path",
            kind=SymbolKind.CLASS,
            package="stdlib",
        )

        # resolve_external should return the richer node from cache
        resolved = graph.resolve_external("stdlib::pathlib.Path")
        assert resolved is not None
        assert resolved.documentation == "Represents a filesystem path."
        assert resolved.signature == "class Path(*args, **kwargs)"


class TestInferPackage:
    """Unit tests for _infer_package heuristics."""

    def test_site_packages(self) -> None:
        graph = CodeGraph()
        result = graph._infer_package(
            "/home/user/.local/lib/python3.13/site-packages/pydantic/main.py"
        )
        assert result == "pydantic"

    def test_stdlib(self) -> None:
        graph = CodeGraph()
        result = graph._infer_package(
            "/usr/lib/python3.13/pathlib.py"
        )
        assert result == "stdlib"

    def test_typeshed(self) -> None:
        graph = CodeGraph()
        result = graph._infer_package(
            "/usr/lib/python3.13/typeshed/stdlib/builtins.pyi"
        )
        assert result == "stdlib"

    def test_venv_site_packages(self) -> None:
        graph = CodeGraph()
        result = graph._infer_package(
            "/home/user/project/.venv/lib/python3.13/site-packages/requests/api.py"
        )
        assert result == "requests"

    def test_unknown_returns_none(self) -> None:
        graph = CodeGraph()
        result = graph._infer_package("/tmp/some_random_file.py")
        assert result is None
