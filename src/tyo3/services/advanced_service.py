"""Advanced semantic analysis service — tyo3-advanced.allium rules (deferred v0.2+)."""

from __future__ import annotations

from typing import Optional

from tyo3.models.advanced import SemanticToken
from tyo3.models.analysis import Range
from tyo3.models.core import ProjectFile, TyProject


def classify_tokens(
    project: TyProject, file: ProjectFile, range: Optional[Range] = None
) -> list[SemanticToken]:
    """Black-box: returns typed tokens using ty_ide::semantic_tokens.

    When range is None, returns tokens for the entire file.
    """
    return []


def prepare_hierarchy(
    project: TyProject, file: ProjectFile, line: int, column: int
) -> Optional[dict]:
    """Black-box: initialises a type hierarchy query using ty_ide::prepare_type_hierarchy."""
    return None


def resolve_supertypes(project: TyProject, item: dict) -> list[dict]:
    """Black-box: traverses supertypes from a prepared hierarchy entry."""
    return []


def resolve_subtypes(project: TyProject, item: dict) -> list[dict]:
    """Black-box: traverses subtypes from a prepared hierarchy entry."""
    return []


class AdvancedService:
    """Implements advanced semantic analysis rules from tyo3-advanced.allium."""

    def __init__(self) -> None:
        self._tokens: list[SemanticToken] = []

    @property
    def tokens(self) -> list[SemanticToken]:
        return list(self._tokens)

    # ── GetSemanticTokens ──────────────────────────────────────────────

    def get_semantic_tokens(
        self,
        project: TyProject,
        file: ProjectFile,
        range: Optional[Range] = None,
    ) -> list[SemanticToken]:
        """GetSemanticTokens: requires project.is_open and file.project == project."""
        if not project.is_open:
            raise ValueError("Project is not open")
        if file.project.root != project.root:
            raise ValueError("File does not belong to project")

        tokens = classify_tokens(project, file, range)
        for t in tokens:
            self._tokens.append(t)
        return tokens

    # ── ExploreTypeHierarchy ───────────────────────────────────────────

    def explore_type_hierarchy(
        self,
        project: TyProject,
        file: ProjectFile,
        line: int,
        column: int,
        direction: Optional[str] = None,
    ) -> dict:
        """ExploreTypeHierarchy: validates preconditions then resolves.

        When direction is None, returns both supertypes and subtypes.
        When direction is 'supertypes' or 'subtypes', returns only that direction.
        """
        if not project.is_open:
            raise ValueError("Project is not open")
        if file.project.root != project.root:
            raise ValueError("File does not belong to project")
        if line < 1 or column < 1:
            raise ValueError("Position must be 1-based (line >= 1, column >= 1)")

        prepared = prepare_hierarchy(project, file, line, column)
        if prepared is None:
            return {
                "item_name": None,
                "item_detail": None,
                "item_path": None,
                "item_full_range": None,
                "item_selection_range": None,
                "supertype_count": 0,
                "subtype_count": 0,
            }

        supertypes = (
            resolve_supertypes(project, prepared)
            if direction in (None, "supertypes")
            else []
        )
        subtypes = (
            resolve_subtypes(project, prepared)
            if direction in (None, "subtypes")
            else []
        )

        return {
            "item_name": prepared.get("name"),
            "item_detail": prepared.get("detail"),
            "item_path": prepared.get("path"),
            "item_full_range": prepared.get("full_range"),
            "item_selection_range": prepared.get("selection_range"),
            "supertype_count": len(supertypes),
            "subtype_count": len(subtypes),
        }
