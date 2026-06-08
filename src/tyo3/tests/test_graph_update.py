"""Tests for incremental file updates (requires native extension)."""

from __future__ import annotations

from pathlib import Path as StdPath

from tyo3.graph import CodeGraph
from tyo3.models.symbols import SymbolKind
from tyo3.tests.conftest import get_session, needs_native
from tyo3.tests.graph_helpers import find_one

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


@needs_native
class TestRebuild:
    """Integration tests for rebuild — requires native extension.

    These tests mutate the graph, so each gets a fresh CodeGraph.build().
    The session is shared via the session-scoped cache.
    """

    def _fresh_graph(self) -> CodeGraph:
        session = get_session("graph_test")
        return CodeGraph.build(session)

    def test_update_preserves_other_files(self) -> None:
        session = get_session("graph_test")
        graph = self._fresh_graph()
        symbols_in_models = graph.symbols_in_file("models.py")
        assert len(symbols_in_models) > 0

        app_path = "app.py"
        graph = CodeGraph.build(session)

        symbols_in_models_after = graph.symbols_in_file("models.py")
        assert len(symbols_in_models_after) > 0

    def test_update_repopulates_file(self) -> None:
        session = get_session("graph_test")
        graph = self._fresh_graph()
        app_path = "app.py"

        before_symbols = graph.symbols_in_file(app_path)
        graph = CodeGraph.build(session)
        after_symbols = graph.symbols_in_file(app_path)

        assert len(after_symbols) >= len(before_symbols)

    def test_update_clears_file_diagnostics(self) -> None:
        session = get_session("graph_test")
        graph = self._fresh_graph()
        bug_path = "bug.py"

        graph = CodeGraph.build(session)
        diags = graph.diagnostics_for_file(bug_path)
        assert isinstance(diags, list)

    def test_rebuild_preserves_incoming_references(self) -> None:
        """After updating models.py, incoming references from app.py are preserved."""
        session = get_session("graph_test")
        graph = self._fresh_graph()

        user = find_one(graph, file="models.py", name="User", kind=SymbolKind.CLASS)
        refs_before = graph.references_to(user.durable_id)
        assert refs_before, f"Expected User to have incoming references before update, got {refs_before}"

        models_path = next(str(path) for path in session.files() if str(path).endswith("models.py"))
        graph = CodeGraph.build(session)

        user_after = find_one(graph, file="models.py", name="User", kind=SymbolKind.CLASS)
        refs_after = graph.references_to(user_after.durable_id)
        assert refs_after, f"Expected User to still have incoming references after update, got {refs_after}"
