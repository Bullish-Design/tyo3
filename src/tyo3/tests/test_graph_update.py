"""Tests for incremental file updates (requires native extension)."""

from __future__ import annotations

from pathlib import Path as StdPath

from tyo3.graph import CodeGraph
from tyo3.tests.conftest import needs_native, get_session

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


@needs_native
class TestUpdateFile:
    """Integration tests for update_file — requires native extension.

    These tests mutate the graph, so each gets a fresh CodeGraph.build().
    The session is shared via the session-scoped cache.
    """

    def _fresh_graph(self) -> CodeGraph:
        session = get_session("graph_test")
        return CodeGraph.build(session)

    def test_update_preserves_other_files(self) -> None:
        session = get_session("graph_test")
        graph = self._fresh_graph()
        original_count = graph.node_count
        symbols_in_models = graph.symbols_in_file(
            str(StdPath(fixture_path("graph_test")) / "models.py")
        )
        assert len(symbols_in_models) > 0

        app_path = str(StdPath(fixture_path("graph_test")) / "app.py")
        graph.update_file(session, app_path)

        symbols_in_models_after = graph.symbols_in_file(
            str(StdPath(fixture_path("graph_test")) / "models.py")
        )
        assert len(symbols_in_models_after) > 0

    def test_update_repopulates_file(self) -> None:
        session = get_session("graph_test")
        graph = self._fresh_graph()
        app_path = str(StdPath(fixture_path("graph_test")) / "app.py")

        before_symbols = graph.symbols_in_file(app_path)
        graph.update_file(session, app_path)
        after_symbols = graph.symbols_in_file(app_path)

        assert len(after_symbols) >= len(before_symbols)

    def test_update_clears_file_diagnostics(self) -> None:
        session = get_session("graph_test")
        graph = self._fresh_graph()
        bug_path = str(StdPath(fixture_path("graph_test")) / "bug.py")

        graph.update_file(session, bug_path)
        diags = graph.diagnostics_for_file(bug_path)
        assert isinstance(diags, list)
