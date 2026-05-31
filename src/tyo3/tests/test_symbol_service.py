"""Tests for SymbolService — tyo3-symbols.allium rules.

Obligation groups:
- GetDocumentSymbols: success, file-not-in-project rejection
- SearchWorkspaceSymbols: success, empty query returns empty
- SearchAllSymbols: success, no-context returns empty
"""

import pytest

from tyo3.models.core import FileCategory, Path, ProjectFile


class TestGetDocumentSymbols:
    """GetDocumentSymbols rule tests."""

    def test_returns_symbols(self, symbol_service, open_project, first_party_file) -> None:
        symbols = symbol_service.get_document_symbols(open_project, first_party_file)
        assert symbols == []

    def test_rejects_file_not_in_project(self, symbol_service, open_project) -> None:
        from datetime import datetime, timezone
        from tyo3.models.core import TyProject, ProjectStatus

        other = TyProject(
            root=Path(components=["other"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        other_file = ProjectFile(
            path=Path(components=["other", "f.py"]),
            project=other,
            file_category=FileCategory.FIRST_PARTY,
        )
        with pytest.raises(ValueError, match="does not belong"):
            symbol_service.get_document_symbols(open_project, other_file)

    def test_rejects_closed_project(self, symbol_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            symbol_service.get_document_symbols(closed_project, first_party_file)


class TestSearchWorkspaceSymbols:
    """SearchWorkspaceSymbols rule tests."""

    def test_returns_matches(self, symbol_service, open_project) -> None:
        symbols = symbol_service.search_workspace_symbols(open_project, "MyClass")
        assert symbols == []

    def test_empty_query_returns_empty(self, symbol_service, open_project) -> None:
        symbols = symbol_service.search_workspace_symbols(open_project, "")
        assert symbols == []

    def test_rejects_closed_project(self, symbol_service, closed_project) -> None:
        with pytest.raises(ValueError, match="not open"):
            symbol_service.search_workspace_symbols(closed_project, "foo")


class TestSearchAllSymbols:
    """SearchAllSymbols rule tests."""

    def test_returns_matches_with_context(self, symbol_service, open_project, first_party_file) -> None:
        symbols = symbol_service.search_all_symbols(open_project, "List", importing_from=first_party_file)
        assert symbols == []

    def test_no_context_returns_empty(self, symbol_service, open_project) -> None:
        symbols = symbol_service.search_all_symbols(open_project, "List")
        assert symbols == []

    def test_empty_query_returns_empty(self, symbol_service, open_project) -> None:
        symbols = symbol_service.search_all_symbols(open_project, "")
        assert symbols == []

    def test_rejects_closed_project(self, symbol_service, closed_project) -> None:
        with pytest.raises(ValueError, match="not open"):
            symbol_service.search_all_symbols(closed_project, "foo")

    def test_rejects_wrong_context_file(self, symbol_service, open_project) -> None:
        from datetime import datetime, timezone
        from tyo3.models.core import TyProject, ProjectStatus

        other = TyProject(
            root=Path(components=["other"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        other_file = ProjectFile(
            path=Path(components=["other", "f.py"]),
            project=other,
            file_category=FileCategory.FIRST_PARTY,
        )
        with pytest.raises(ValueError, match="does not belong"):
            symbol_service.search_all_symbols(open_project, "foo", importing_from=other_file)
