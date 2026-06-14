"""Phase 7 tests: pinned snapshot graphs and live HEAD graph deltas."""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

from tyo3 import TyO3Session
from tyo3.graph import CodeGraph


def _node_ids(graph: CodeGraph) -> set[str]:
    raw = graph.graph
    return {raw[i].durable_id for i in raw.node_indices()}


def _node_names(graph: CodeGraph) -> set[str]:
    raw = graph.graph
    return {raw[i].name for i in raw.node_indices()}


def test_session_graph_updates_after_edit(tmp_path: StdPath) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as session:
        graph = session.graph
        assert graph.revision == session.head

        result = session.edit("a.py", "x = 1\ny = 2\n")

        # Build-on-demand (Project 31 #1b): the commit dropped the prior graph;
        # the next access rebuilds a fresh graph at the new revision.
        new_graph = session.graph
        assert new_graph is not graph
        assert new_graph.revision == result.revision
        assert "y" in _node_names(new_graph)


def test_snapshot_graph_is_pinned_across_head_edits(tmp_path: StdPath) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as session:
        session.sync_all()  # Populate identity registry so symbols have durable_ids
        before = session.snapshot()
        try:
            pinned = before.graph()
            before_nodes = _node_ids(pinned)
            before_names = _node_names(pinned)

            session.edit("a.py", "x = 1\ny = 2\n")

            assert _node_ids(before.graph()) == before_nodes
            assert before.graph().revision == before.revision
            assert before.graph().frozen
            assert "y" not in before_names
            assert "y" in _node_names(session.graph)
        finally:
            before.close()


def test_snapshot_graph_copy_does_not_mutate_head_graph(tmp_path: StdPath) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as session:
        head_graph = session.graph
        with session.snapshot() as snap:
            pinned = snap.graph()
            with pytest.raises(RuntimeError, match="revision-pinned"):
                pinned.apply_code_delta({"revision": 999, "rescan": False})

        assert session.graph is head_graph


def test_graph_diff_reports_added_symbol(tmp_path: StdPath) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as session:
        session.sync_all()  # Populate identity registry so symbols have durable_ids
        before = session.snapshot()
        try:
            before_graph = before.graph()
            session.edit("a.py", "x = 1\ny = 2\n")
            with session.snapshot() as after:
                after_graph = after.graph()
        finally:
            before.close()

        diff = after_graph.diff(before_graph)
        assert diff.changed
        assert any(node.name == "y" for node in diff.added_nodes)
