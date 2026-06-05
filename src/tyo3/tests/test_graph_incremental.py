"""Tests for incremental graph updates via apply_delta (Phase 6)."""

from __future__ import annotations

import textwrap
from pathlib import Path as StdPath

import pytest

from tyo3 import TyO3Session
from tyo3.graph import CodeGraph, EdgeKind
from tyo3.tests.graph_helpers import edges_of_kind


# ── Parity helpers ──────────────────────────────────────────────────────


def _node_ids(g: CodeGraph) -> set[str]:
    return {g.graph[i].durable_id for i in g.graph.node_indices()}


def _edge_triples(g: CodeGraph) -> set[tuple[str, str, str]]:
    """(source_id, target_id, edge_kind) for every edge — order-independent."""
    out: set[tuple[str, str, str]] = set()
    for ei in g.graph.edge_indices():
        data = g.graph.get_edge_data_by_index(ei)
        s, t = g.graph.get_edge_endpoints_by_index(ei)
        out.add((g.graph[s].durable_id, g.graph[t].durable_id, str(data.kind)))
    return out


def _assert_structurally_equal(a: CodeGraph, b: CodeGraph) -> None:
    assert _node_ids(a) == _node_ids(b), (
        f"node id mismatch\nonly in delta: {_node_ids(a) - _node_ids(b)}\n"
        f"only in rebuild: {_node_ids(b) - _node_ids(a)}"
    )
    assert _edge_triples(a) == _edge_triples(b), (
        f"edge mismatch\nonly in delta: {_edge_triples(a) - _edge_triples(b)}\n"
        f"only in rebuild: {_edge_triples(b) - _edge_triples(a)}"
    )


def _build(session: TyO3Session) -> CodeGraph:
    """Build a graph, ensuring the identity registry is populated first."""
    # id_for() requires reconciliation which happens during sync_all() / edit().
    # Ensure the identity system is initialized before the first build.
    if not hasattr(session, '_graph_identity_primed'):
        session.sync_all()
        session._graph_identity_primed = True
    return CodeGraph.build(session)


# ── Core parity tests ───────────────────────────────────────────────────


def test_apply_delta_changed_file_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text(
        "from models import User\n\n\ndef run():\n    return User().save()\n"
    )
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        # change models.py: add a method (new node + edges; importers unaffected
        # structurally except their refs still resolve).
        sync = s.edit(
            "models.py", "class User:\n    def save(self): ...\n    def load(self): ...\n"
        )
        g.apply_delta(s, sync)  # session provides id_for for DurableId derivation
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_apply_delta_created_file_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "app.py").write_text("X = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        sync = s.edit("helpers.py", "def helper():\n    return 42\n")  # Created
        assert sync.created, "expected a created file in the delta"
        g.apply_delta(s, sync)  # session provides id_for for DurableId derivation
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_apply_delta_deleted_file_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        # delete app.py from disk, then ingest the deletion.
        (tmp_path / "app.py").unlink()
        sync = s.sync_path("app.py")
        assert sync.deleted, "expected a deleted file in the delta"
        g.apply_delta(s, sync)  # session provides id_for for DurableId derivation
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_apply_delta_revalidates_inbound_cross_file_edges(tmp_path: StdPath) -> None:
    """Changing models.py must keep app.py's references INTO models.py correct."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text(
        "from models import User\n\n\ndef run():\n    return User().save()\n"
    )
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        sync = s.edit(
            "models.py",
            "class User:\n    def save(self): ...\n    def extra(self): ...\n",
        )
        g.apply_delta(s, sync)  # session provides id_for for DurableId derivation
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)
        # explicit: app.py still imports models.py after the change
        assert ("app.py", "models.py") in {
            (a.split("::")[0], b.split("::")[0])
            for a, b in edges_of_kind(g, EdgeKind.IMPORTS)
        }


def test_apply_delta_rescan_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        sync = s.sync_all()
        assert sync.rescan
        g.apply_delta(s, sync)  # session provides id_for for DurableId derivation
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


# ── Reverse-dependency index unit tests ──────────────────────────────────


def test_importers_index_populated_after_build(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        assert "app.py" in g._importers_of({"models.py"})
        assert g._importers_of({"models.py"}) == g._file_importers.get("models.py", set())


def test_importers_index_survives_apply_delta(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        sync = s.edit("models.py", "class User:\n    name: str\n")
        g.apply_delta(s, sync)  # session provides id_for for DurableId derivation
        assert "app.py" in g._importers_of({"models.py"})  # rebuilt, not lost


# ── Idempotence / sequence ───────────────────────────────────────────────


def test_apply_delta_sequence_matches_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        for text in (
            "class User:\n    a: int\n",
            "class User:\n    a: int\n    b: int\n",
            "class User:\n    b: int\n",
        ):
            sync = s.edit("models.py", text)
            g.apply_delta(s, sync)  # session provides id_for for DurableId derivation
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


# ── Guard: apply_delta without build ────────────────────────────────────


def test_apply_delta_without_build_raises(tmp_path: StdPath) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        sync = s.edit("a.py", "x = 2\n")
        g = CodeGraph()
        with pytest.raises(RuntimeError, match="CodeGraph\\.build"):
            g.apply_delta(s, sync)
