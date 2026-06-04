"""TyO3Session — unified project session API.

Replaces the separate ProjectService, AnalysisService, SymbolService,
and NavigationService with a single coherent interface.
"""

from __future__ import annotations

from pathlib import Path as StdPath
from pathlib import PurePosixPath

from tyo3.models.advanced import SemanticToken
from tyo3.models.analysis import CheckResult
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverResult,
    NameOccurrence,
    Reference,
    TypeHierarchy,
)
from tyo3.models.symbols import Symbol
from tyo3.rust_project import RustProject


class TyO3Session:
    """A live session with the ty semantic engine for a single project root.

    Usage::

        with TyO3Session("/path/to/project") as session:
            result = session.check()
            symbols = session.document_symbols("src/main.py")
            definitions = session.goto_definition("src/main.py", 10, 5)
    """

    def __init__(self, root: str | StdPath) -> None:
        self._rp = RustProject(root)
        self._root = self._rp.root

    def __enter__(self) -> TyO3Session:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ── Lifecycle ────────────────────────────────────────────

    def reload(self) -> None:
        """Reload the project, clearing cached state."""
        self._rp.reload()

    def close(self) -> None:
        """Close the session and free Rust-side resources."""
        self._rp.close()

    @property
    def root(self) -> StdPath:
        return self._root

    # ── Files ────────────────────────────────────────────────

    def files(self) -> list[PurePosixPath]:
        """Return all file paths known to the project."""
        return self._rp.files()

    # ── Analysis ─────────────────────────────────────────────

    def check(self) -> CheckResult:
        """Run the type checker on the entire project."""
        return self._rp.check()

    def check_file(self, path: str | StdPath) -> CheckResult:
        """Run the type checker and return diagnostics for a single file.

        Runs a full project check, then filters diagnostics in Rust
        before they cross the boundary — only diagnostics for the
        target file are returned.
        """
        return self._rp.check_file(path)

    # ── Symbols ──────────────────────────────────────────────

    def document_symbols(self, path: str | StdPath) -> list[Symbol]:
        """Return symbols defined in the given file."""
        return self._rp.document_symbols(path)

    def workspace_symbols(self, query: str) -> list[Symbol]:
        """Search for symbols matching query across the project."""
        if len(query) < 1:
            return []
        return self._rp.workspace_symbols(query)

    # ── Navigation ───────────────────────────────────────────

    def goto_definition(self, path: str | StdPath, line: int, column: int) -> list[DefinitionTarget]:
        """Navigate to the definition of the symbol at (line, column)."""
        return self._rp.goto_definition(path, line, column)

    def goto_declaration(self, path: str | StdPath, line: int, column: int) -> list[DefinitionTarget]:
        """Navigate to the declaration of the symbol at (line, column)."""
        return self._rp.goto_declaration(path, line, column)

    def goto_type_definition(self, path: str | StdPath, line: int, column: int) -> list[DefinitionTarget]:
        """Navigate to the type definition of the symbol at (line, column)."""
        return self._rp.goto_type_definition(path, line, column)

    def find_references(
        self,
        path: str | StdPath,
        line: int,
        column: int,
        include_declaration: bool = True,
    ) -> list[Reference]:
        """Find all references to the symbol at (line, column)."""
        return self._rp.find_references(path, line, column, include_declaration)

    def hover(self, path: str | StdPath, line: int, column: int) -> HoverResult | None:
        """Get hover information for the symbol at (line, column)."""
        return self._rp.hover(path, line, column)

    # ── Type Hierarchy ───────────────────────────────────────

    def type_hierarchy(self, path: str | StdPath, line: int, column: int) -> TypeHierarchy | None:
        """Query type hierarchy at a position."""
        return self._rp.type_hierarchy(path, line, column)

    # ── Semantic Tokens ───────────────────────────────────────

    def semantic_tokens(self, path: str | StdPath) -> list[SemanticToken]:
        """Return semantic tokens for a file."""
        return self._rp.semantic_tokens(path)

    # ── File Occurrences ─────────────────────────────────────

    def file_occurrences(self, path: str | StdPath) -> list[NameOccurrence]:
        """Batch-resolve all name occurrences in a file.

        Returns every name-like token in the file, each resolved to
        its definition target and classified by reference role
        (read, write, import, definition, or other).
        """
        return self._rp.file_occurrences(path)
