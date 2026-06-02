"""Symbol discovery service — tyo3-symbols.allium rules.

DEPRECATED: Use tyo3.TyO3Session instead. This module will be removed in v0.2.
"""

from __future__ import annotations

from tyo3.models.core import ProjectFile, TyProject
from tyo3.models.symbols import Symbol


def document_symbols(file: ProjectFile) -> list[Symbol]:
    """Black-box: returns all symbols defined in a file.

    In production this calls ty_ide::document_symbols.
    """
    return []


def workspace_symbols(project: TyProject, query: str) -> list[Symbol]:
    """Black-box: searches first-party project symbols matching query.

    Uses ty_ide::workspace_symbols with fuzzy matching.
    """
    return []


def all_symbols(project: TyProject, query: str, context_file: ProjectFile) -> list[Symbol]:
    """Black-box: searches all importable symbols matching query.

    Uses ty_ide::all_symbols.
    """
    return []


def first_project_file(project: TyProject) -> ProjectFile | None:
    """Returns the first indexed first-party file, or None."""
    return None


class SymbolService:
    """Implements symbol discovery rules from tyo3-symbols.allium.

    Parameters
    ----------
    use_rust:
        When ``True``, symbol queries are backed by the Rust ty engine via
        :class:`~tyo3.rust_project.RustProject`.  Default ``False`` keeps
        existing black-box stubs for unit testing.
    """

    def __init__(self, use_rust: bool = False) -> None:
        self._symbols: list[Symbol] = []
        self._use_rust: bool = use_rust
        # RustProject instances keyed by root path string
        self._rust_projects: dict[str, object] = {}

    # ── Rust backend access ─────────────────────────────────────────

    def _get_rust_project(self, root_path: str) -> object | None:
        """Return the RustProject for *root_path*, or ``None``."""
        return self._rust_projects.get(root_path)

    def set_rust_project(self, root_path: str, rp: object) -> None:
        """Register a RustProject instance for dependency injection."""
        self._rust_projects[root_path] = rp

    @property
    def symbols(self) -> list[Symbol]:
        return list(self._symbols)

    # ── GetDocumentSymbols ─────────────────────────────────────────────

    def get_document_symbols(self, project: TyProject, file: ProjectFile) -> list[Symbol]:
        """GetDocumentSymbols: requires project.is_open and file.project == project."""
        if not project.is_open:
            raise ValueError("Project is not open")
        if file.project.root != project.root:
            raise ValueError("File does not belong to project")

        rp = self._get_rust_project(str(project.root))
        if rp is not None and self._use_rust:
            file_path = str(file.path)
            symbols = rp.document_symbols(file_path)  # type: ignore[union-attr]
            self._symbols.extend(symbols)
            return symbols

        symbols = document_symbols(file)
        self._symbols.extend(symbols)
        return symbols

    # ── SearchWorkspaceSymbols ─────────────────────────────────────────

    def search_workspace_symbols(self, project: TyProject, query: str) -> list[Symbol]:
        """SearchWorkspaceSymbols: requires project.is_open and len(query) >= 1."""
        if not project.is_open:
            raise ValueError("Project is not open")
        if len(query) < 1:
            return []

        rp = self._get_rust_project(str(project.root))
        if rp is not None and self._use_rust:
            matches = rp.workspace_symbols(query)  # type: ignore[union-attr]
            self._symbols.extend(matches)
            return matches

        matches = workspace_symbols(project, query)
        self._symbols.extend(matches)
        return matches

    # ── SearchAllSymbols ───────────────────────────────────────────────

    def search_all_symbols(
        self,
        project: TyProject,
        query: str,
        importing_from: ProjectFile | None = None,
    ) -> list[Symbol]:
        """SearchAllSymbols: requires project.is_open and len(query) >= 1."""
        if not project.is_open:
            raise ValueError("Project is not open")
        if len(query) < 1:
            return []

        context_file = importing_from if importing_from is not None else first_project_file(project)
        if context_file is None:
            return []
        if context_file.project.root != project.root:
            raise ValueError("Context file does not belong to project")

        # NOTE: all_symbols is not callable from Rust (QueryPattern not
        # publicly exported from ty_ide).  Always fall back to stub.
        matches = all_symbols(project, query, context_file)
        self._symbols.extend(matches)
        return matches
