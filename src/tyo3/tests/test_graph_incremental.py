"""Tests for incremental HEAD-graph updates via the native code delta (Phase 4).

The live HEAD graph (``session.graph``) is maintained across writes by the native
post-commit path (``_apply_graph_delta`` → ``apply_code_delta`` / a full-delta
rebuild). These tests assert that the incrementally-maintained head graph stays
**structurally equal** to a fresh native ``CodeGraph.build`` of the same revision
— the cutover's core guarantee. (The pure applier itself is unit-tested in
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


# ── Core parity tests: incrementally-maintained head == fresh native build ──


def test_changed_file_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text("from models import User\n\n\ndef run():\n    return User().save()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph  # live HEAD graph (native projection)
        s.edit("models.py", "class User:\n    def save(self): ...\n    def load(self): ...\n")
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_created_file_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "app.py").write_text("X = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        sync = s.edit("helpers.py", "def helper():\n    return 42\n")  # Created
        assert sync.created, "expected a created file in the delta"
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_deleted_file_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        (tmp_path / "app.py").unlink()
        sync = s.sync_path("app.py")
        assert sync.deleted, "expected a deleted file in the delta"
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


def test_revalidates_inbound_cross_file_edges(tmp_path: StdPath) -> None:
    """Changing models.py must keep app.py's references INTO models.py correct."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text("from models import User\n\n\ndef run():\n    return User().save()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        s.edit("models.py", "class User:\n    def save(self): ...\n    def extra(self): ...\n")
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)

        def _file(did: str) -> str:
            return did.removeprefix("<module>").split("::")[0]

        assert ("app.py", "models.py") in {(_file(a), _file(b)) for a, b in edges_of_kind(g, EdgeKind.IMPORTS)}


def test_rescan_equals_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        sync = s.sync_all()
        assert sync.rescan
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)


# ── Reverse-dependency index ──────────────────────────────────────────────


def test_importers_index_populated_after_build(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        assert "app.py" in g._file_importers.get("models.py", set())


def test_importers_index_survives_edit(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        s.edit("models.py", "class User:\n    name: str\n")
        assert "app.py" in g._file_importers.get("models.py", set())  # maintained, not lost


# ── Sequence ─────────────────────────────────────────────────────────────


def test_edit_sequence_matches_rebuild(tmp_path: StdPath) -> None:
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        for text in (
            "class User:\n    a: int\n",
            "class User:\n    a: int\n    b: int\n",
            "class User:\n    b: int\n",
        ):
            s.edit("models.py", text)
        rebuilt = CodeGraph.build(s)
        _assert_structurally_equal(g, rebuilt)
