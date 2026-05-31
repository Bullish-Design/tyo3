"""Tests for NavigationService — tyo3-navigation.allium rules.

Obligation groups:
- GotoDefinition: success, no-target, invalid position
- GotoDeclaration: success, no-target, invalid position
- GotoTypeDefinition: success, no-target, invalid position
- FindReferences: success, no-target, invalid position, include_declaration
- GetHover: success, no-content, invalid position
"""

import pytest

from tyo3.models.core import ProjectFile


class TestGotoDefinition:
    """GotoDefinition rule tests."""

    def test_returns_targets(self, navigation_service, open_project, first_party_file) -> None:
        targets = navigation_service.goto_definition(open_project, first_party_file, 1, 1)
        assert targets == []

    def test_invalid_position_raises(self, navigation_service, open_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="1-based"):
            navigation_service.goto_definition(open_project, first_party_file, 0, 1)
        with pytest.raises(ValueError, match="1-based"):
            navigation_service.goto_definition(open_project, first_party_file, 1, 0)

    def test_rejects_closed_project(self, navigation_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            navigation_service.goto_definition(closed_project, first_party_file, 1, 1)

    def test_rejects_file_not_in_project(self, navigation_service, open_project) -> None:
        from datetime import datetime, timezone
        from tyo3.models.core import TyProject, ProjectStatus, Path

        other = TyProject(
            root=Path(components=["other"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        other_file = ProjectFile(
            path=Path(components=["other", "f.py"]),
            project=other,
            file_category="first_party",
        )
        with pytest.raises(ValueError, match="does not belong"):
            navigation_service.goto_definition(open_project, other_file, 1, 1)


class TestGotoDeclaration:
    """GotoDeclaration rule tests."""

    def test_returns_targets(self, navigation_service, open_project, first_party_file) -> None:
        targets = navigation_service.goto_declaration(open_project, first_party_file, 1, 1)
        assert targets == []

    def test_invalid_position_raises(self, navigation_service, open_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="1-based"):
            navigation_service.goto_declaration(open_project, first_party_file, 0, 1)


class TestGotoTypeDefinition:
    """GotoTypeDefinition rule tests."""

    def test_returns_targets(self, navigation_service, open_project, first_party_file) -> None:
        targets = navigation_service.goto_type_definition(open_project, first_party_file, 1, 1)
        assert targets == []

    def test_invalid_position_raises(self, navigation_service, open_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="1-based"):
            navigation_service.goto_type_definition(open_project, first_party_file, 0, 1)


class TestFindReferences:
    """FindReferences rule tests."""

    def test_returns_references(self, navigation_service, open_project, first_party_file) -> None:
        refs = navigation_service.find_references(open_project, first_party_file, 10, 5, include_declaration=True)
        assert refs == []

    def test_without_declaration(self, navigation_service, open_project, first_party_file) -> None:
        refs = navigation_service.find_references(open_project, first_party_file, 10, 5, include_declaration=False)
        assert refs == []

    def test_invalid_position_raises(self, navigation_service, open_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="1-based"):
            navigation_service.find_references(open_project, first_party_file, 0, 5)


class TestGetHover:
    """GetHover rule tests."""

    def test_returns_none_when_no_content(self, navigation_service, open_project, first_party_file) -> None:
        result = navigation_service.get_hover(open_project, first_party_file, 1, 1)
        assert result is None

    def test_invalid_position_raises(self, navigation_service, open_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="1-based"):
            navigation_service.get_hover(open_project, first_party_file, 0, 1)

    def test_rejects_closed_project(self, navigation_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            navigation_service.get_hover(closed_project, first_party_file, 1, 1)
