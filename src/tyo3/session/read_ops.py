"""``_ReadOps`` — the read-only analysis surface shared by the session, its
snapshots, and the floating-latest view.

Split from ``session.py`` (Phase 13). Pure move: every read method is unchanged.
"""

from __future__ import annotations

from pathlib import Path as StdPath
from pathlib import PurePosixPath
from typing import Any

from tyo3.exceptions import (
    InternalTyError,
    PathResolutionError,
    PositionError,
    ProjectClosedError,
)
from tyo3.models.advanced import SemanticToken
from tyo3.models.analysis import CheckResult, Range
from tyo3.models.editor import FoldingRange, Hint, InlayHint
from tyo3.models.lsp import Completion, SignatureHelp
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverResult,
    NameOccurrence,
    QuickFix,
    Reference,
    TypeHierarchy,
    TypeHierarchyItem,
    WorkspaceEdit,
)
from tyo3.models.symbols import Symbol
from tyo3.session._native import (
    _NativeClosedError,
    _NativePathError,
    _NativePositionError,
)


class _ReadOps:
    """Read-only analysis methods shared by TyO3Session and Snapshot.

    Subclasses must provide ``self._inner`` (native handle) and ``self._closed``.
    """

    _inner: Any
    _closed: bool

    def _check_open(self) -> None:
        """Raise ProjectClosedError if this handle has been closed."""
        if self._closed:
            raise ProjectClosedError("Project is closed")

    def _native(self) -> Any:
        """The native handle that read methods dispatch to. Overridden by
        TyO3Session to return a cached head snapshot; Snapshot uses itself."""
        return self._inner

    # ── Files ────────────────────────────────────────────────────────

    def files(self) -> list[PurePosixPath]:
        """Return the file paths known to this project."""
        self._check_open()
        try:
            raw: list[str] = self._native().files()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in files(): {e}") from e
        return [PurePosixPath(p) for p in raw]

    # ── Check ────────────────────────────────────────────────────────

    def check(self) -> CheckResult:
        """Run the type-checker and return structured diagnostics."""
        self._check_open()
        try:
            native_result = self._native().check()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in check(): {e}") from e

        return CheckResult.model_validate(native_result)

    def check_file(self, path: str | StdPath) -> CheckResult:
        """Run the type-checker and return diagnostics for a single file."""
        self._check_open()
        try:
            native_result = self._native().check_file(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in check_file(): {e}") from e

        return CheckResult.model_validate(native_result)

    # ── Document Symbols ─────────────────────────────────────────────

    def document_symbols(self, path: str | StdPath) -> list[Symbol]:
        """Return symbols defined in the given file."""
        self._check_open()
        try:
            native_symbols = self._native().document_symbols(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in document_symbols(): {e}") from e

        return [Symbol.model_validate(s) for s in native_symbols]

    # ── Workspace Symbols ────────────────────────────────────────────

    def workspace_symbols(self, query: str) -> list[Symbol]:
        """Search for symbols matching *query* across the project."""
        self._check_open()
        if not query:
            return []
        try:
            native_symbols = self._native().workspace_symbols(query)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in workspace_symbols(): {e}") from e

        return [Symbol.model_validate(s) for s in native_symbols]

    # ── Goto Definition ──────────────────────────────────────────────

    def goto_definition(self, path: str | StdPath, line: int, column: int) -> list[DefinitionTarget]:
        """Navigate to the definition of the symbol at *(line, column)*."""
        return self._goto("goto_definition", path, line, column)

    # ── Goto Declaration ─────────────────────────────────────────────

    def goto_declaration(self, path: str | StdPath, line: int, column: int) -> list[DefinitionTarget]:
        """Navigate to the declaration of the symbol at *(line, column)*."""
        return self._goto("goto_declaration", path, line, column)

    # ── Goto Type Definition ─────────────────────────────────────────

    def goto_type_definition(self, path: str | StdPath, line: int, column: int) -> list[DefinitionTarget]:
        """Navigate to the type definition of the symbol at *(line, column)*."""
        return self._goto("goto_type_definition", path, line, column)

    def _goto(self, method: str, path: str | StdPath, line: int, column: int) -> list[DefinitionTarget]:
        """Shared implementation for all goto-* methods."""
        self._check_open()
        try:
            native_targets = getattr(self._native(), method)(str(path), line, column)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in {method}(): {e}") from e

        return [DefinitionTarget.model_validate(t) for t in native_targets]

    # ── Find References ─────────────────────────────────────────────

    def find_references(
        self,
        path: str | StdPath,
        line: int,
        column: int,
        include_declaration: bool = True,
    ) -> list[Reference]:
        """Find all references to the symbol at *(line, column)*."""
        self._check_open()
        try:
            native_refs = self._native().find_references(str(path), line, column, include_declaration)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in find_references(): {e}") from e

        return [Reference.model_validate(r) for r in native_refs]

    # ── Document Highlights ───────────────────────────────────────

    def document_highlights(self, path: str | StdPath, line: int, column: int) -> list[Reference]:
        """Highlight all in-file occurrences of the symbol at *(line, column)*.

        Like ``find_references`` but scoped to the current file. Returns an
        empty list when no symbol is highlightable at the position.
        """
        self._check_open()
        try:
            native_refs = self._native().document_highlights(str(path), line, column)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in document_highlights(): {e}") from e

        return [Reference.model_validate(r) for r in native_refs]

    # ── Rename ───────────────────────────────────────────────────────

    def can_rename(self, path: str | StdPath, line: int, column: int) -> Range | None:
        """Return the editable range if the symbol at *(line, column)* can be renamed."""
        self._check_open()
        try:
            native_range = self._native().can_rename(str(path), line, column)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in can_rename(): {e}") from e

        if native_range is None:
            return None
        return Range.model_validate(native_range)

    def rename(self, path: str | StdPath, line: int, column: int, new_name: str) -> WorkspaceEdit | None:
        """Compute the workspace edit to rename the symbol at *(line, column)*."""
        self._check_open()
        try:
            native_edit = self._native().rename(str(path), line, column, new_name)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in rename(): {e}") from e

        if native_edit is None:
            return None
        return WorkspaceEdit.model_validate(native_edit)

    # ── Selection Ranges ───────────────────────────────────────────

    def selection_ranges(self, path: str | StdPath, line: int, column: int) -> list[Range]:
        """Compute selection ranges at *(line, column)*."""
        self._check_open()
        try:
            native_ranges = self._native().selection_ranges(str(path), line, column)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in selection_ranges(): {e}") from e

        return [Range.model_validate(r) for r in native_ranges]

    # ── Folding Ranges ─────────────────────────────────────────────

    def folding_ranges(self, path: str | StdPath) -> list[FoldingRange]:
        """Return folding ranges for a file."""
        self._check_open()
        try:
            native_ranges = self._native().folding_ranges(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in folding_ranges(): {e}") from e

        return [FoldingRange.model_validate(r) for r in native_ranges]

    # ── Inlay Hints ──────────────────────────────────────────────────

    def inlay_hints(self, path: str | StdPath) -> list[InlayHint]:
        """Return inlay hints for a file."""
        self._check_open()
        try:
            result = self._native().inlay_hints(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in inlay_hints(): {e}") from e

        return [InlayHint.model_validate(r) for r in result]

    # ── Hints ────────────────────────────────────────────────────────

    def hints(self, path: str | StdPath) -> list[Hint]:
        """Return hints (unused bindings, unreachable code) for a file."""
        self._check_open()
        try:
            result = self._native().hints(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in hints(): {e}") from e

        return [Hint.model_validate(r) for r in result]

    # ── Code Actions ─────────────────────────────────────────────────

    def code_actions(
        self,
        path: str | StdPath,
        start_line: int,
        start_col: int,
        end_line: int,
        end_col: int,
        diagnostic_id: str,
    ) -> list[QuickFix]:
        """Get quick fixes for a diagnostic at a range."""
        self._check_open()
        try:
            result = self._native().code_actions(str(path), start_line, start_col, end_line, end_col, diagnostic_id)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in code_actions(): {e}") from e

        return [QuickFix.model_validate(r) for r in result]

    # ── Signature Help ──────────────────────────────────────────────

    def signature_help(self, path: str | StdPath, line: int, column: int) -> SignatureHelp | None:
        """Get signature help at *(line, column)*."""
        self._check_open()
        try:
            result = self._native().signature_help(str(path), line, column)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in signature_help(): {e}") from e

        if result is None:
            return None
        return SignatureHelp.model_validate(result)

    # ── Completion ─────────────────────────────────────────────────

    def completions(self, path: str | StdPath, line: int, column: int, *, auto_import: bool = True) -> list[Completion]:
        """Get completion suggestions at *(line, column)*."""
        self._check_open()
        try:
            result = self._native().completions(str(path), line, column, auto_import=auto_import)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in completions(): {e}") from e

        return [Completion.model_validate(r) for r in result]

    # ── Semantic Tokens ──────────────────────────────────────────

    def semantic_tokens(
        self,
        path: str | StdPath,
        *,
        start_line: int | None = None,
        start_col: int | None = None,
        end_line: int | None = None,
        end_col: int | None = None,
    ) -> list[SemanticToken]:
        """Return semantic tokens for a file, optionally scoped to a range."""
        self._check_open()
        try:
            native_result = self._native().semantic_tokens(
                str(path),
                start_line=start_line,
                start_col=start_col,
                end_line=end_line,
                end_col=end_col,
            )
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in semantic_tokens(): {e}") from e

        path_posix = PurePosixPath(str(path))
        result = []
        for d in native_result:
            d["file"] = path_posix
            result.append(SemanticToken.model_validate(d))
        return result

    # ── File Occurrences ─────────────────────────────────────────

    def file_occurrences(self, path: str | StdPath) -> list[NameOccurrence]:
        """Batch-resolve all name occurrences in a file.

        Returns every name-like token in the file, each resolved to
        its definition target and classified by reference role.

        This replaces per-token ``goto_definition`` calls with a single
        Rust call — O(1) FFI calls instead of O(tokens).
        """
        self._check_open()
        try:
            native_result = self._native().file_occurrences(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in file_occurrences(): {e}") from e

        return [NameOccurrence.model_validate(o) for o in native_result]

    # ── Identity (Gate 2) ────────────────────────────────────────────
    # Available on TyO3Session; Snapshot may not support these if the
    # native snapshot handle doesn't expose identity methods.

    def id_for(self, path: str, line: int, col: int):
        """Resolve the DurableId of the entity at (path, line, col).

        Returns None if no entity was found or not supported.
        """
        self._check_open()
        try:
            return self._native().id_for(path, line, col)
        except Exception:
            return None

    def locate(self, durable_id: str):
        """Locate the current file::qualified_path for a DurableId.

        Returns None if the id is not in the registry or not supported.
        """
        self._check_open()
        try:
            return self._native().locate(durable_id)
        except Exception:
            return None

    def needs_review(self) -> list[str]:
        """List durable ids currently flagged as NeedsReview.

        Returns empty list if the native handle doesn't support it.
        """
        self._check_open()
        try:
            return self._native().needs_review()
        except Exception:
            return []

    def orphaned(self) -> list[str]:
        """List durable ids currently flagged as Orphaned.

        Returns empty list if the native handle doesn't support it.
        """
        self._check_open()
        try:
            return self._native().orphaned()
        except Exception:
            return []

    # ── Hover ────────────────────────────────────────────────────────

    def hover(self, path: str | StdPath, line: int, column: int) -> HoverResult | None:
        """Get hover information for the symbol at *(line, column)*.

        Returns ``None`` when no hover information is available.
        """
        self._check_open()
        try:
            native_hover = self._native().hover(str(path), line, column)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in hover(): {e}") from e

        if native_hover is None:
            return None

        return HoverResult.model_validate(native_hover)

    # ── Type Hierarchy ───────────────────────────────────────────

    def type_hierarchy(self, path: str | StdPath, line: int, column: int) -> TypeHierarchy | None:
        """Query type hierarchy at a position."""
        self._check_open()
        try:
            native_result = self._native().type_hierarchy(str(path), line, column)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in type_hierarchy(): {e}") from e

        if native_result is None:
            return None
        return TypeHierarchy.model_validate(native_result)

    # ── Class Supertypes ─────────────────────────────────────────

    def class_supertypes(self, path: str | StdPath, line: int, column: int) -> list[TypeHierarchyItem]:
        """Return the direct base classes of the class at *(line, column)*.

        Lean counterpart to :meth:`type_hierarchy` for graph construction:
        resolves only supertypes and skips the expensive project-wide subtype
        scan (which walks every module, including typeshed). Returns an empty
        list when the position is not on a class.
        """
        self._check_open()
        try:
            native_result = self._native().class_supertypes(str(path), line, column)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except OverflowError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in class_supertypes(): {e}") from e

        return [TypeHierarchyItem.model_validate(o) for o in native_result]


# ── _OwnedView — a convenience layer view that owns its snapshot ─────────────


