"""Tests for AdvancedService — tyo3-advanced.allium rules.

Obligation groups:
- GetSemanticTokens: success, file-not-in-project rejection
- ExploreTypeHierarchy: success, no-target, directional queries
- Deferred spec awareness (SemanticTokens.full, TypeHierarchy.explore)
"""

from datetime import UTC
from pathlib import PurePosixPath

import pytest

from tyo3.models.core import FileCategory, ProjectFile


class TestGetSemanticTokens:
    """GetSemanticTokens rule tests."""

    def test_returns_tokens(self, advanced_service, open_project, first_party_file) -> None:
        tokens = advanced_service.get_semantic_tokens(open_project, first_party_file)
        assert tokens == []

    def test_rejects_file_not_in_project(self, advanced_service, open_project) -> None:
        from datetime import datetime

        from tyo3.models.core import ProjectStatus, TyProject

        other = TyProject(
            root=PurePosixPath("other"),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(UTC),
        )
        other_file = ProjectFile(
            path=PurePosixPath("other/f.py"),
            project=other,
            file_category=FileCategory.FIRST_PARTY,
        )
        with pytest.raises(ValueError, match="does not belong"):
            advanced_service.get_semantic_tokens(open_project, other_file)

    def test_rejects_closed_project(self, advanced_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            advanced_service.get_semantic_tokens(closed_project, first_party_file)


class TestExploreTypeHierarchy:
    """ExploreTypeHierarchy rule tests."""

    def test_no_target_returns_nulls(self, advanced_service, open_project, first_party_file) -> None:
        result = advanced_service.explore_type_hierarchy(open_project, first_party_file, 1, 1)
        assert result["item_name"] is None
        assert result["supertype_count"] == 0
        assert result["subtype_count"] == 0

    def test_directional_supertypes(self, advanced_service, open_project, first_party_file) -> None:
        result = advanced_service.explore_type_hierarchy(open_project, first_party_file, 1, 1, direction="supertypes")
        assert result["supertype_count"] == 0
        assert result["subtype_count"] == 0

    def test_directional_subtypes(self, advanced_service, open_project, first_party_file) -> None:
        result = advanced_service.explore_type_hierarchy(open_project, first_party_file, 1, 1, direction="subtypes")
        assert result["supertype_count"] == 0
        assert result["subtype_count"] == 0

    def test_invalid_position_raises(self, advanced_service, open_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="1-based"):
            advanced_service.explore_type_hierarchy(open_project, first_party_file, 0, 1)

    def test_rejects_closed_project(self, advanced_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            advanced_service.explore_type_hierarchy(closed_project, first_party_file, 1, 1)
