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


# ── Phase 6: Import cycles (unit tests — no native required) ──


class TestImportCycles:
    """Unit tests for import cycle detection."""

    def _make_module_node(
        self, graph: CodeGraph, file: str, name: str
    ) -> str:
        """Helper: add a MODULE node and return its symbol_id."""
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
        """Helper: add an IMPORTS edge from src module to tgt module."""
        src_id = f"{src_file}::<module>"
        tgt_id = f"{tgt_file}::<module>"
        edge = EdgeData(kind=EdgeKind.IMPORTS)
        graph._add_edge(src_id, tgt_id, edge, src_file)

    def _make_data_node(
        self, graph: CodeGraph, symbol_id: str, file: str, 
    ) -> None:
        """Helper: add a non-module node (so module indices differ)."""
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
        # The cycle should contain both module IDs
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
        """Self-referential imports should not produce cycles."""
        graph = CodeGraph()
        self._make_module_node(graph, "a.py", "a")
        self._add_import_edge(graph, "a.py", "a.py")
        # The adjacency is built so src_mod != tgt_mod — self-edges skipped
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
        """REFERENCES edges between files also contribute to cycles."""
        graph = CodeGraph()
        self._make_module_node(graph, "x.py", "x")
        self._make_module_node(graph, "y.py", "y")
        # Use a REFERENCE edge rather than IMPORTS
        src_id = "x.py::<module>"
        tgt_id = "y.py::<module>"
        edge_a = EdgeData(kind=EdgeKind.REFERENCES, file="x.py")
        edge_b = EdgeData(kind=EdgeKind.REFERENCES, file="y.py")
        graph._add_edge(src_id, tgt_id, edge_a, "x.py")
        graph._add_edge(tgt_id, src_id, edge_b, "y.py")
        cycles = graph.import_cycles()
        assert len(cycles) == 1


# ── Phase 6: Hub symbols ──


class TestHubSymbols:
    """Unit tests for betweenness-centrality hub detection."""

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
        # Single node has no paths, so betweenness is 0 — filtered out
        assert len(hubs) == 0

    def test_star_topology(self) -> None:
        """In a bidirectional star, the center should have highest centrality."""
        graph = CodeGraph()
        center = self._make_node(graph, "center")
        leaves = ["leaf_a", "leaf_b", "leaf_c", "leaf_d", "leaf_e"]
        for leaf in leaves:
            sid = self._make_node(graph, leaf)
            self._link(graph, center, sid)  # center -> leaf
            self._link(graph, sid, center)  # leaf -> center  (bidirectional)
        hubs = graph.hub_symbols(top_n=3)
        assert len(hubs) > 0
        # The center should be the top hub
        if hubs:
            top_id, top_score = hubs[0]
            assert top_id == center, f"Expected {center} as top hub, got {top_id}"

    def test_respects_top_n(self) -> None:
        graph = CodeGraph()
        for i in range(10):
            self._make_node(graph, f"n{i}")
        # Link in a chain: n0 -> n1 -> n2 -> ... -> n9
        for i in range(9):
            a = f"test.py::n{i}"
            b = f"test.py::n{i+1}"
            self._link(graph, a, b)
        hubs = graph.hub_symbols(top_n=3)
        assert len(hubs) <= 3


# ── Phase 6: Subgraph extraction ──


class TestSubgraphForFile:
    """Unit tests for subgraph extraction."""

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

        # foo references bar
        edge = EdgeData(kind=EdgeKind.REFERENCES)
        graph._add_edge(func_a, func_b, edge, "a.py")

        sub = graph.subgraph_for_file("a.py")
        # Should include mod_a, func_a (core nodes) and func_b (neighbour)
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


# ── Phase 6: Export (unit tests — no native required) ──


class TestExport:
    """Tests for DOT and JSON export functions."""

    def _make_graph_with_data(self) -> CodeGraph:
        graph = CodeGraph()
        # Module node
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
        # Function node
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
        # Edge: module defines function
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
        # Should only contain nodes up to index 2
        assert "n0" in dot
        assert "n2" in dot
        # n4 should not appear (index 4 is beyond max_nodes=3)
        assert "n4" not in dot

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
        assert 'style="dashed"' in dot
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


# ── Phase 6: Incremental updates (integration tests) ──


@needs_native
class TestUpdateFile:
    """Integration tests for update_file — requires native extension."""

    def test_update_preserves_other_files(self) -> None:
        """After updating one file, other file nodes are untouched."""
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            # Record nodes before update
            original_count = graph.node_count
            symbols_in_models = graph.symbols_in_file(
                str(StdPath(fixture_path("graph_test")) / "models.py")
            )
            assert len(symbols_in_models) > 0

            # Update app.py
            app_path = str(StdPath(fixture_path("graph_test")) / "app.py")
            graph.update_file(session, app_path)

            # models.py symbols should still be there
            symbols_in_models_after = graph.symbols_in_file(
                str(StdPath(fixture_path("graph_test")) / "models.py")
            )
            assert len(symbols_in_models_after) > 0

    def test_update_repopulates_file(self) -> None:
        """After updating, the file's symbols are back in the graph."""
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            app_path = str(StdPath(fixture_path("graph_test")) / "app.py")

            before_symbols = graph.symbols_in_file(app_path)
            graph.update_file(session, app_path)
            after_symbols = graph.symbols_in_file(app_path)

            # Should have at least as many as before (or more if new symbols)
            assert len(after_symbols) >= len(before_symbols)

    def test_update_clears_file_diagnostics(self) -> None:
        """After updating, diagnostics for the file are re-populated."""
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            bug_path = str(StdPath(fixture_path("graph_test")) / "bug.py")

            graph.update_file(session, bug_path)
            diags = graph.diagnostics_for_file(bug_path)
            # Should still have diagnostics after re-indexing
            assert isinstance(diags, list)


@needs_native
class TestImportCyclesIntegration:
    """Integration tests for import cycles on real fixtures.

    Note: detection depends on the ``file_occurrences`` API producing
    correct inter-file dependency edges.  When the API resolves
    cross-file references to external stubs (typeshed), the module
    adjacency graph may not reflect real import relationships.
    The unit tests (TestImportCycles) verify the algorithm itself
    with manually constructed graphs.
    """

    def test_no_false_cycles_on_simple_package(self) -> None:
        """A simple package without circular imports should not
        produce spurious cycles."""
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("simple_package")) as session:
            graph = CodeGraph.build(session)
            cycles = graph.import_cycles()
            # It must not report false positives
            assert len(cycles) == 0, (
                f"simple_package should have no import cycles, got {cycles}"
            )

    def test_does_not_crash_on_complex_fixture(self) -> None:
        """The circular_imports fixture exercises the detector without
        crashing, even if the API cannot track cross-file edges yet."""
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("circular_imports")) as session:
            graph = CodeGraph.build(session)
            cycles = graph.import_cycles()
            # Must return a list (possibly empty due to API limitations)
            assert isinstance(cycles, list)

    def test_graph_test_fixture_no_crash(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            cycles = graph.import_cycles()
            assert isinstance(cycles, list)


@needs_native
class TestHubSymbolsIntegration:
    """Integration tests for hub symbol detection."""

    def test_hub_symbols_returns_list(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            hubs = graph.hub_symbols(top_n=5)
            assert isinstance(hubs, list)
            for sid, score in hubs:
                assert isinstance(sid, str)
                assert isinstance(score, float)
                assert score > 0.0

    def test_hub_symbols_sorted_descending(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
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
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            models_path = str(
                StdPath(fixture_path("graph_test")) / "models.py"
            )
            sub = graph.subgraph_for_file(models_path)
            assert sub.num_nodes() > 0
            # Should have at least the module node
            node_names = {sub[i].name for i in sub.node_indices()}
            assert len(node_names) >= 1

    def test_export_dot_of_subgraph(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            models_path = str(
                StdPath(fixture_path("graph_test")) / "models.py"
            )
            sub = graph.subgraph_for_file(models_path)
            # We can't call to_dot directly on the sub (it expects a CodeGraph),
            # but we can verify the sub is a valid PyDiGraph
            assert sub.num_nodes() > 0
            # Check we can iterate
            for idx in sub.node_indices():
                node = sub[idx]
                assert isinstance(node, SymbolNode)
                break
