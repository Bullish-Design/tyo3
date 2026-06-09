"""Tests for DependencyGraph save/load and external symbol resolution."""

from __future__ import annotations

import pytest
import rustworkx as rx

from tyo3.graph import CodeGraph, DependencyGraph, EdgeData, EdgeKind, SymbolNode
from tyo3.models.analysis import Range
from tyo3.models.navigation import ReferenceRole
from tyo3.models.symbols import SymbolKind


class TestDependencyGraph:
    def test_construction_and_lookup(self) -> None:
        g = rx.PyDiGraph()
        node = SymbolNode(
            durable_id="stdlib::json.loads",
            name="loads",
            qualified_name="json.loads",
            kind=SymbolKind.FUNCTION,
            file="<external>",
            range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
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
        g = rx.PyDiGraph()
        n1 = SymbolNode(
            durable_id="pkg::A",
            name="A",
            qualified_name="A",
            kind=SymbolKind.CLASS,
            file="<external>",
            range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
            external=True,
            package="pkg",
        )
        n2 = SymbolNode(
            durable_id="pkg::B",
            name="B",
            qualified_name="B",
            kind=SymbolKind.CLASS,
            file="<external>",
            range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
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
        monkeypatch.setattr("tyo3.graph.dependency.CACHE_DIR", tmp_path / "tyo3_deps")

        g = rx.PyDiGraph()
        node = SymbolNode(
            durable_id="testpkg::Foo",
            name="Foo",
            qualified_name="Foo",
            kind=SymbolKind.CLASS,
            file="<external>",
            range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
            external=True,
            package="testpkg",
        )
        idx = g.add_node(node)
        id_to_index = {"testpkg::Foo": idx}

        dep = DependencyGraph("testpkg", "2.0", g, id_to_index)
        saved_path = dep.save()
        assert saved_path.exists()

        loaded = DependencyGraph.load("testpkg", "2.0")
        assert loaded is not None
        assert loaded.package == "testpkg"
        assert loaded.version == "2.0"

        found = loaded.lookup("testpkg::Foo")
        assert found is not None
        assert found.name == "Foo"
        assert found.external is True
        assert found.package == "testpkg"

    def test_dependency_graph_edge_role_roundtrips(self, tmp_path, monkeypatch) -> None:
        """Loaded dependency graph EdgeData.role is ReferenceRole, not raw string."""
        monkeypatch.setattr("tyo3.graph.dependency.CACHE_DIR", tmp_path / "tyo3_deps")

        g = rx.PyDiGraph()
        n1 = SymbolNode(
            durable_id="testpkg::A",
            name="A",
            qualified_name="A",
            kind=SymbolKind.CLASS,
            file="<external>",
            range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
            external=True,
            package="testpkg",
        )
        n2 = SymbolNode(
            durable_id="testpkg::B",
            name="B",
            qualified_name="B",
            kind=SymbolKind.FUNCTION,
            file="<external>",
            range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
            external=True,
            package="testpkg",
        )
        idx1 = g.add_node(n1)
        idx2 = g.add_node(n2)
        g.add_edge(
            idx1,
            idx2,
            EdgeData(
                kind=EdgeKind.REFERENCES,
                role=ReferenceRole.READ,
                file="<external>",
            ),
        )
        id_to_index = {"testpkg::A": idx1, "testpkg::B": idx2}

        dep = DependencyGraph("testpkg", "3.0", g, id_to_index)
        dep.save()

        loaded = DependencyGraph.load("testpkg", "3.0")
        assert loaded is not None

        # Verify the edge role is ReferenceRole, not a raw string
        for edge_idx in loaded.graph.edge_indices():
            edge_data = loaded.graph.get_edge_data_by_index(edge_idx)
            assert edge_data is not None
            assert edge_data.role == ReferenceRole.READ
            assert isinstance(edge_data.role, ReferenceRole)
            # It must not be the raw string "read"
            # (ReferenceRole.READ compares equal to "read" since it's a StrEnum,
            #  but isinstance() above already proved it's the right type)
            break
        else:
            pytest.fail("No edges found in loaded graph")

    def test_load_nonexistent_returns_none(self) -> None:
        result = DependencyGraph.load("nonexistent_pkg", "99.9")
        assert result is None

    def test_list_cached(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("tyo3.graph.dependency.CACHE_DIR", tmp_path / "tyo3_deps")

        for pkg, ver in [("alpha", "1.0"), ("beta", "2.0")]:
            g = rx.PyDiGraph()
            node = SymbolNode(
                durable_id=f"{pkg}::X",
                name="X",
                qualified_name="X",
                kind=SymbolKind.CLASS,
                file="<external>",
                range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
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
        monkeypatch.setattr("tyo3.graph.dependency.CACHE_DIR", tmp_path / "nonexistent_dir")
        result = DependencyGraph.list_cached()
        assert result == []


class TestResolveExternal:
    def test_returns_same_node_when_not_external(self) -> None:
        graph = CodeGraph()
        node = SymbolNode(
            durable_id="src/main.py::foo",
            name="foo",
            qualified_name="foo",
            kind=SymbolKind.FUNCTION,
            file="src/main.py",
            range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
            external=False,
        )
        graph._add_node(node)
        result = graph.resolve_external("src/main.py::foo")
        assert result is not None
        assert result.external is False

    def test_returns_stub_when_no_cache(self) -> None:
        graph = CodeGraph()
        # External stub nodes are materialised by the native CodeDelta with their
        # package already set (§6.2.2); inject one directly to exercise the
        # no-cache path of resolve_external.
        graph._add_node(
            SymbolNode(
                durable_id="unknown::Bar",
                name="Bar",
                qualified_name="Bar",
                kind=SymbolKind.CLASS,
                file="<external>",
                range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
                external=True,
                package="unknown",
            )
        )
        result = graph.resolve_external("unknown::Bar")
        assert result is not None
        assert result.external is True
        assert result.package == "unknown"

    def test_resolves_from_cache(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("tyo3.graph.dependency.CACHE_DIR", tmp_path / "tyo3_deps")

        g = rx.PyDiGraph()
        # The cached node carries richer (but still slim, §6.2.2) data than the
        # bare stub — here a real content_hash and a resolved file. resolve_external
        # must return this cached node, not the stub.
        rich_node = SymbolNode(
            durable_id="stdlib::pathlib.Path",
            name="Path",
            qualified_name="pathlib.Path",
            kind=SymbolKind.CLASS,
            file="stdlib/pathlib.pyi",
            range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
            external=True,
            package="stdlib",
            content_hash="cafebabe",
        )
        idx = g.add_node(rich_node)
        id_to_index = {"stdlib::pathlib.Path": idx}
        dep = DependencyGraph("stdlib", "unknown", g, id_to_index)
        dep.save()

        graph = CodeGraph()
        graph._add_node(
            SymbolNode(
                durable_id="stdlib::pathlib.Path",
                name="Path",
                qualified_name="pathlib.Path",
                kind=SymbolKind.CLASS,
                file="<external>",
                range=Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}),
                external=True,
                package="stdlib",
            )
        )

        resolved = graph.resolve_external("stdlib::pathlib.Path")
        assert resolved is not None
        # Came from the cache (richer fields), not the bare stub.
        assert resolved.content_hash == "cafebabe"
        assert resolved.file == "stdlib/pathlib.pyi"


# NOTE: the old ``TestInferPackage`` suite exercised ``CodeGraph._infer_package``,
# a Python path→package heuristic. Package attribution now happens authoritatively
# in the native CodeDelta producer (external nodes carry ``package`` directly), so
# that helper and its tests were removed with the Gate 3N build rewrite.
