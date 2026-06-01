"""Code navigation service — tyo3-navigation.allium rules."""

from __future__ import annotations

from typing import Optional

from tyo3.models.core import ProjectFile, TyProject
from tyo3.models.navigation import DefinitionTarget, HoverResult, Reference


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
    """Implements code navigation rules from tyo3-navigation.allium.

    Parameters
    ----------
    use_rust:
        When ``True``, navigation queries are backed by the Rust ty engine via
        :class:`~tyo3.rust_project.RustProject`.  Default ``False`` keeps
        existing black-box stubs for unit testing.
    """

    def __init__(self, use_rust: bool = False) -> None:
        self._use_rust: bool = use_rust
        # RustProject instances keyed by root path string
        self._rust_projects: dict[str, object] = {}

    # ── Rust backend access ─────────────────────────────────────────

    def _get_rust_project(self, root_path: str) -> Optional[object]:
        """Return the RustProject for *root_path*, or ``None``."""
        return self._rust_projects.get(root_path)

    def set_rust_project(self, root_path: str, rp: object) -> None:
        """Register a RustProject instance for dependency injection."""
        self._rust_projects[root_path] = rp

    @staticmethod
    def _path_to_str(path: Path) -> str:
        """Convert a Path model to a file-system path string.

        Handles the root ``/`` component correctly (avoids ``//`` prefix).
        """
        components = path.components
        if components and components[0] == "/":
            return "/" + "/".join(components[1:])
        return "/".join(components)

    def _file_path_str(self, file: ProjectFile) -> str:
        """Convert a ProjectFile's path to a file-system path string."""
        return self._path_to_str(file.path)

    # ── GotoDefinition ─────────────────────────────────────────────────

    def goto_definition(
        self, project: TyProject, file: ProjectFile, line: int, column: int
    ) -> list[DefinitionTarget]:
        """GotoDefinition: validates preconditions, then resolves."""
        self._validate_common(project, file, line, column)

        rp = self._get_rust_project(str(project.root))
        if rp is not None and self._use_rust:
            targets = rp.goto_definition(  # type: ignore[union-attr]
                self._file_path_str(file), line, column
            )
            for t in targets:
                t.project = project
            return targets

        return resolve_definition(project, file, line, column)

    # ── GotoDeclaration ────────────────────────────────────────────────

    def goto_declaration(
        self, project: TyProject, file: ProjectFile, line: int, column: int
    ) -> list[DefinitionTarget]:
        """GotoDeclaration: validates preconditions, then resolves."""
        self._validate_common(project, file, line, column)

        rp = self._get_rust_project(str(project.root))
        if rp is not None and self._use_rust:
            targets = rp.goto_declaration(  # type: ignore[union-attr]
                self._file_path_str(file), line, column
            )
            for t in targets:
                t.project = project
            return targets

        return resolve_declaration(project, file, line, column)

    # ── GotoTypeDefinition ─────────────────────────────────────────────

    def goto_type_definition(
        self, project: TyProject, file: ProjectFile, line: int, column: int
    ) -> list[DefinitionTarget]:
        """GotoTypeDefinition: validates preconditions, then resolves."""
        self._validate_common(project, file, line, column)

        rp = self._get_rust_project(str(project.root))
        if rp is not None and self._use_rust:
            targets = rp.goto_type_definition(  # type: ignore[union-attr]
                self._file_path_str(file), line, column
            )
            for t in targets:
                t.project = project
            return targets

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

        rp = self._get_rust_project(str(project.root))
        if rp is not None and self._use_rust:
            refs = rp.find_references(  # type: ignore[union-attr]
                self._file_path_str(file), line, column, include_declaration
            )
            for r in refs:
                r.project = project
            return refs

        return resolve_references(project, file, line, column, include_declaration)

    # ── GetHover ───────────────────────────────────────────────────────

    def get_hover(
        self, project: TyProject, file: ProjectFile, line: int, column: int
    ) -> Optional[HoverResult]:
        """GetHover: validates preconditions, then resolves."""
        self._validate_common(project, file, line, column)

        rp = self._get_rust_project(str(project.root))
        if rp is not None and self._use_rust:
            return rp.hover(  # type: ignore[union-attr]
                self._file_path_str(file), line, column
            )

        result = resolve_hover(project, file, line, column)
        if result is None:
            return None
        # Convert dict to HoverResult (legacy path)
        from tyo3.models.navigation import HoverContent, HoverContentKind
        from tyo3.models.analysis import FileRange

        contents = [
            HoverContent(
                kind=HoverContentKind(c.get("kind", "plain_text")),
                value=c.get("value", ""),
            )
            for c in result.get("contents", [])
        ]
        return HoverResult(
            location=result.get("location", FileRange(path=file.path, range=result.get("range", None))),
            contents=contents,
        )

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
