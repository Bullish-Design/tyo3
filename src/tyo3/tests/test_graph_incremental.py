"""Tests for the build-on-demand HEAD graph (Project 31, #1b).

The HEAD graph (``session.graph``) is no longer maintained incrementally across
writes: each commit drops it (``_invalidate_head_snap``) and the next access
rebuilds it on demand from a full native ``full_code_delta()``. These tests
assert that the rebuilt head graph is **structurally equal** to a fresh native
``CodeGraph.build`` of the same revision — the on-demand build is correct after
every write kind. (The pure applier itself is unit-tested in
``test_graph_apply_code_delta.py``; producer↔legacy parity in
``test_final_parity_oracle.py``.)
"""

from __future__ import annotations

from pathlib import Path as StdPath

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
        f"node id mismatch\nonly in head: {_node_ids(a) - _node_ids(b)}\nonly in rebuild: {_node_ids(b) - _node_ids(a)}"
    )
    assert _edge_triples(a) == _edge_triples(b), (
        f"edge mismatch\nonly in head: {_edge_triples(a) - _edge_triples(b)}\n"
        f"only in rebuild: {_edge_triples(b) - _edge_triples(a)}"
    )


# ── Core parity tests: on-demand head graph == fresh native build ──────────
#
# Each test reads ``s.graph`` *after* the mutation, so it exercises the
# build-on-demand rebuild at the new revision (the head graph captured before a
# write is intentionally dropped by the commit).


def test_changed_file_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text("from models import User\n\n\ndef run():\n    return User().save()\n")
    with TyO3Session(str(tmp_path)) as s:
        s.edit("models.py", "class User:\n    def save(self): ...\n    def load(self): ...\n")
        g = s.graph  # rebuilt on demand at the new revision
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_created_file_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "app.py").write_text("X = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        sync = s.edit("helpers.py", "def helper():\n    return 42\n")  # Created
        assert sync.created, "expected a created file in the delta"
        g = s.graph
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_deleted_file_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        (tmp_path / "app.py").unlink()
        sync = s.sync_path("app.py")
        assert sync.deleted, "expected a deleted file in the delta"
        g = s.graph
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_revalidates_inbound_cross_file_edges(tmp_path: StdPath) -> None:
    """Changing models.py must keep app.py's references INTO models.py correct."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text("from models import User\n\n\ndef run():\n    return User().save()\n")
    with TyO3Session(str(tmp_path)) as s:
        s.edit("models.py", "class User:\n    def save(self): ...\n    def extra(self): ...\n")
        g = s.graph
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)

        def _file(did: str) -> str:
            return did.removeprefix("<module>").split("::")[0]

        assert ("app.py", "models.py") in {(_file(a), _file(b)) for a, b in edges_of_kind(g, EdgeKind.IMPORTS)}


def test_rescan_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        sync = s.sync_all()
        assert sync.rescan
        g = s.graph
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


# ── Reverse-dependency index ──────────────────────────────────────────────


def test_importers_index_populated_after_build(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        assert "app.py" in g._file_importers.get("models.py", set())


def test_importers_index_populated_after_edit(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        s.edit("models.py", "class User:\n    name: str\n")
        g = s.graph  # rebuilt on demand — index repopulated wholesale
        assert "app.py" in g._file_importers.get("models.py", set())


# ── Sequence ─────────────────────────────────────────────────────────────


def test_edit_sequence_matches_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        for text in (
            "class User:\n    a: int\n",
            "class User:\n    a: int\n    b: int\n",
            "class User:\n    b: int\n",
        ):
            s.edit("models.py", text)
        g = s.graph
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_graph_rebuilt_after_each_commit(tmp_path: StdPath) -> None:
    """``session.graph`` reflects the new revision after every write — and is a
    distinct instance (build-on-demand drops the prior graph)."""
    (tmp_path / "m.py").write_text("class User: ...\n")
    with TyO3Session(str(tmp_path)) as s:
        g0 = s.graph
        rev0 = g0.revision
        s.edit("m.py", "class User:\n    def save(self): ...\n")
        g1 = s.graph
        assert g1 is not g0, "head graph must be rebuilt (not the same maintained instance)"
        assert g1.revision == s.head and g1.revision != rev0
        _assert_structurally_equal(g1, CodeGraph.build(s))
