"""Code navigation service — tyo3-navigation.allium rules."""

from __future__ import annotations

from typing import Optional

from tyo3.models.core import ProjectFile, TyProject
from tyo3.models.navigation import DefinitionTarget, Reference


def resolve_definition(
    project: TyProject, file: ProjectFile, line: int, column: int
) -> list[DefinitionTarget]:
    """Black-box: resolves definition location(s), using ty_ide::goto_definition."""
    return []


def resolve_declaration(
    project: TyProject, file: ProjectFile, line: int, column: int
) -> list[DefinitionTarget]:
    """Black-box: resolves declaration location(s), using ty_ide::goto_declaration."""
    return []


def resolve_type_definition(
    project: TyProject, file: ProjectFile, line: int, column: int
) -> list[DefinitionTarget]:
    """Black-box: resolves type definition location(s), using ty_ide::goto_type_definition."""
    return []


def resolve_references(
    project: TyProject,
    file: ProjectFile,
    line: int,
    column: int,
    include_declaration: bool,
) -> list[Reference]:
    """Black-box: resolves reference occurrences, using ty_ide::find_references."""
    return []


def resolve_hover(
    project: TyProject, file: ProjectFile, line: int, column: int
) -> Optional[dict]:
    """Black-box: returns structured hover content, using ty_ide::hover."""
    return None


class NavigationService:
    """Implements code navigation rules from tyo3-navigation.allium."""

    # ── GotoDefinition ─────────────────────────────────────────────────

    def goto_definition(
        self, project: TyProject, file: ProjectFile, line: int, column: int
    ) -> list[DefinitionTarget]:
        """GotoDefinition: validates preconditions, then resolves."""
        self._validate_common(project, file, line, column)
        return resolve_definition(project, file, line, column)

    # ── GotoDeclaration ────────────────────────────────────────────────

    def goto_declaration(
        self, project: TyProject, file: ProjectFile, line: int, column: int
    ) -> list[DefinitionTarget]:
        """GotoDeclaration: validates preconditions, then resolves."""
        self._validate_common(project, file, line, column)
        return resolve_declaration(project, file, line, column)

    # ── GotoTypeDefinition ─────────────────────────────────────────────

    def goto_type_definition(
        self, project: TyProject, file: ProjectFile, line: int, column: int
    ) -> list[DefinitionTarget]:
        """GotoTypeDefinition: validates preconditions, then resolves."""
        self._validate_common(project, file, line, column)
        return resolve_type_definition(project, file, line, column)

    # ── FindReferences ─────────────────────────────────────────────────

    def find_references(
        self,
        project: TyProject,
        file: ProjectFile,
        line: int,
        column: int,
        include_declaration: bool = True,
    ) -> list[Reference]:
        """FindReferences: validates preconditions, then resolves."""
        self._validate_common(project, file, line, column)
        return resolve_references(project, file, line, column, include_declaration)

    # ── GetHover ───────────────────────────────────────────────────────

    def get_hover(
        self, project: TyProject, file: ProjectFile, line: int, column: int
    ) -> Optional[dict]:
        """GetHover: validates preconditions, then resolves."""
        self._validate_common(project, file, line, column)
        return resolve_hover(project, file, line, column)

    # ── Validation ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_common(
        project: TyProject, file: ProjectFile, line: int, column: int
    ) -> None:
        if not project.is_open:
            raise ValueError("Project is not open")
        if file.project.root != project.root:
            raise ValueError("File does not belong to project")
        if line < 1 or column < 1:
            raise ValueError("Position must be 1-based (line >= 1, column >= 1)")
