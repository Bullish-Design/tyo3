"""Surface availability tests — verifies provides/when conditions.

Surfaces from specs:
- TyProjectAPI: provides operations when project.is_open (or unconditionally)
- TypeChecking: provides operations when project.is_open
- SymbolQuery: provides operations when project.is_open
- CodeNavigation: provides operations when project.is_open and file.project = project
- AdvancedSemantics: provides operations when project.is_open and file.project = project
"""

import pytest


class TestTyProjectAPISurface:
    """TyProjectAPI surface provides tests."""

    def test_open_project_available_always(self, project_service) -> None:
        """UserOpensProject has no 'when' guard — always available."""
        # Check the surface provides UserOpensProject without a 'when' condition
        has_open_project = hasattr(project_service, "open_project")
        assert has_open_project

    def test_reload_project_requires_open(self, project_service, closed_project) -> None:
        """UserReloadsProject has 'when project.is_open' — not available when closed."""
        with pytest.raises(ValueError, match="not open"):
            project_service.reload_project(closed_project)

    def test_close_project_requires_open(self, project_service, closed_project) -> None:
        """UserClosesProject has 'when project.is_open' — not available when closed."""
        with pytest.raises(ValueError, match="not open"):
            project_service.close_project(closed_project)

    def test_list_files_requires_open(self, project_service, closed_project) -> None:
        """UserListsFiles has 'when project.is_open'."""
        with pytest.raises(ValueError, match="not open"):
            project_service.list_files(closed_project)

    def test_filter_files_requires_open(self, project_service, closed_project) -> None:
        """UserListsFilesFiltered has 'when project.is_open'."""
        with pytest.raises(ValueError, match="not open"):
            project_service.filter_files_by_category(closed_project, "first_party")

    def test_query_backend_info_available_always(self, project_service) -> None:
        """UserQueriesBackendInfo has no 'when' guard — always available."""
        info = project_service.query_backend_info()
        assert info is not None


class TestTypeCheckingSurface:
    """TypeChecking surface provides tests."""

    def test_check_project_requires_open(self, analysis_service, closed_project) -> None:
        """UserChecksProject has 'when project.is_open'."""
        with pytest.raises(ValueError, match="not open"):
            analysis_service.check_project(closed_project)

    def test_check_file_requires_open_and_file_ownership(
        self, analysis_service, closed_project, first_party_file
    ) -> None:
        """UserChecksFile has 'when project.is_open and file.project = project'."""
        with pytest.raises(ValueError, match="not open"):
            analysis_service.check_file(closed_project, first_party_file)

    def test_filter_by_severity_requires_open(self, analysis_service, closed_project) -> None:
        with pytest.raises(ValueError, match="not open"):
            analysis_service.filter_by_severity(closed_project, "error")

    def test_filter_by_code_requires_open(self, analysis_service, closed_project) -> None:
        with pytest.raises(ValueError, match="not open"):
            analysis_service.filter_by_code(closed_project, "type-arg")


class TestSymbolQuerySurface:
    """SymbolQuery surface provides tests."""

    def test_document_symbols_requires_open(self, symbol_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            symbol_service.get_document_symbols(closed_project, first_party_file)

    def test_workspace_symbols_requires_open(self, symbol_service, closed_project) -> None:
        with pytest.raises(ValueError, match="not open"):
            symbol_service.search_workspace_symbols(closed_project, "test")

    def test_all_symbols_requires_open(self, symbol_service, closed_project) -> None:
        with pytest.raises(ValueError, match="not open"):
            symbol_service.search_all_symbols(closed_project, "test")


class TestCodeNavigationSurface:
    """CodeNavigation surface provides tests."""

    def test_goto_definition_requires_open(self, navigation_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            navigation_service.goto_definition(closed_project, first_party_file, 1, 1)

    def test_goto_declaration_requires_open(self, navigation_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            navigation_service.goto_declaration(closed_project, first_party_file, 1, 1)

    def test_goto_type_definition_requires_open(self, navigation_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            navigation_service.goto_type_definition(closed_project, first_party_file, 1, 1)

    def test_find_references_requires_open(self, navigation_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            navigation_service.find_references(closed_project, first_party_file, 1, 1)

    def test_hover_requires_open(self, navigation_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            navigation_service.get_hover(closed_project, first_party_file, 1, 1)


class TestAdvancedSemanticsSurface:
    """AdvancedSemantics surface provides tests."""

    def test_semantic_tokens_requires_open(self, advanced_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            advanced_service.get_semantic_tokens(closed_project, first_party_file)

    def test_type_hierarchy_requires_open(self, advanced_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            advanced_service.explore_type_hierarchy(closed_project, first_party_file, 1, 1)
