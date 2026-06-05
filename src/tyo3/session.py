"""TyO3Session — unified project session API.

Single public class wrapping the Rust TyProject PyO3 class with
Pydantic model validation. Each Rust method returns native Python
objects (dicts/lists via pythonize), which are validated directly
through Pydantic's ``model_validate``.

Usage::

    from tyo3 import TyO3Session

    with TyO3Session("/path/to/project") as session:
        result = session.check()
        symbols = session.document_symbols("src/main.py")
        definitions = session.goto_definition("src/main.py", 10, 5)
"""

from __future__ import annotations

import warnings
from pathlib import Path as StdPath
from pathlib import PurePosixPath
from typing import Any

from tyo3.exceptions import (
    InternalTyError,
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
)
from tyo3.models.advanced import SemanticToken
from tyo3.models.analysis import CheckResult, Range, SyncResult
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

# ── Native extension import ──────────────────────────────────────────────
#
# The PyO3 extension module — must match
# #[pyo3(name = "_native_impl")] in rust/src/lib.rs
# The .so is placed inside the tyo3 Python package at src/tyo3/_native_impl.cpython-*.so

try:
    from tyo3 import _native_impl as _native
except ImportError:
    _native = None  # type: ignore[assignment]

# Import typed exception classes so we can catch Rust errors without string matching.
try:
    from tyo3._native_impl import (
        PathResolutionError as _NativePathError,
    )
    from tyo3._native_impl import (
        PositionError as _NativePositionError,
    )
    from tyo3._native_impl import (
        ProjectClosedError as _NativeClosedError,
    )
except ImportError:
    # When the native extension isn't built, define dummy classes
    # that never match in `except` clauses.
    class _NativeClosedError(Exception):  # type: ignore[no-redef]
        pass

    class _NativePathError(Exception):  # type: ignore[no-redef]
        pass

    class _NativePositionError(Exception):  # type: ignore[no-redef]
        pass


# ── _ReadOps — shared read-only analysis base ──────────────────────────────


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

    def document_highlights(
        self, path: str | StdPath, line: int, column: int
    ) -> list[Reference]:
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

    def rename(
        self, path: str | StdPath, line: int, column: int, new_name: str
    ) -> WorkspaceEdit | None:
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

    def selection_ranges(
        self, path: str | StdPath, line: int, column: int
    ) -> list[Range]:
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
            result = self._native().code_actions(
                str(path), start_line, start_col, end_line, end_col, diagnostic_id
            )
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

    def signature_help(
        self, path: str | StdPath, line: int, column: int
    ) -> SignatureHelp | None:
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

    def completions(
        self, path: str | StdPath, line: int, column: int, *, auto_import: bool = True
    ) -> list[Completion]:
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

    def class_supertypes(
        self, path: str | StdPath, line: int, column: int
    ) -> list[TypeHierarchyItem]:
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


# ── TyO3Session ────────────────────────────────────────────────────────────


class TyO3Session(_ReadOps):
    """A live session with the ty semantic engine for a single project root.

    Owns an internal handle to the ``ProjectDatabase`` via the PyO3 boundary.
    All data returned by Rust methods is already-native Python (dicts/lists
    via pythonize) and validated directly through Pydantic's ``model_validate``.

    Session read methods release the GIL during analysis, so they are
    non-blocking: a ``check()`` on one thread no longer freezes other threads
    or the event loop.

    Usage::

        with TyO3Session("/path/to/project") as session:
            result = session.check()
            symbols = session.document_symbols("src/main.py")
            definitions = session.goto_definition("src/main.py", 10, 5)
    """

    def __init__(self, root: str | StdPath) -> None:
        if _native is None:
            raise ProjectOpenError("Rust native extension is not built. Run `devenv shell -- build` first.")
        root_str = str(root)
        try:
            self._inner = _native.TyProject.open(root_str)
        except Exception as e:
            raise ProjectOpenError(f"Cannot open project at '{root_str}': {e}") from e
        self._root = StdPath(root_str).resolve()
        self._closed = False
        self._head_snap: Any = None  # cached native head snapshot (current revision)

    @property
    def root(self) -> StdPath:
        return self._root

    @property
    def head(self) -> int:
        """The current application revision."""
        self._check_open()
        return self._inner.head

    @property
    def latest(self) -> LatestView:
        """A floating, warm read view of the live HEAD (Phase 9).

        ``session.latest.check()`` reflects the newest revision, warm.
        Contrast ``session.snapshot()`` (pinned, cold, isolated).
        See :class:`LatestView`."""
        self._check_open()
        try:
            native_head_view = self._inner.head_view()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in latest: {e}") from e
        return LatestView(native_head_view)

    # ── Head snapshot caching ───────────────────────────────────────

    def _native(self) -> Any:
        """A head snapshot pinned at the current revision, built lazily and
        reused across reads until the next mutation invalidates it. Because
        snapshots are independent (own Zalsa), holding it does NOT block a
        later edit."""
        self._check_open()
        if self._head_snap is None:
            self._head_snap = self._inner.snapshot(None)
        return self._head_snap

    def _invalidate_head_snap(self) -> None:
        """Drop the cached head snapshot reference after a mutation so the
        next read re-pins at the new revision.

        Does NOT forcibly close the old native snapshot — any in-flight read
        that already grabbed it may finish; Python drops it when the last
        reference is gone. This trades prompt cache cleanup for thread safety
        (Phase 5 §5.1)."""
        self._head_snap = None

    # ── Snapshot ─────────────────────────────────────────────────────

    def snapshot(self, at: int | None = None) -> Snapshot:
        """Pin an explicit MVCC snapshot. ``at=None`` pins the current
        revision; ``at=r`` time-travels to a still-retained revision."""
        self._check_open()
        try:
            native_snapshot = self._inner.snapshot(at)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in snapshot(): {e}") from e
        return Snapshot(native_snapshot)

    # ── Write path ────────────────────────────────────────────────────

    def edit(self, path: str | StdPath, text: str) -> SyncResult:
        """Overlay ``path`` with in-memory ``text`` (no disk write).
        Returns a SyncResult with the new revision and affected paths."""
        self._check_open()
        self._invalidate_head_snap()
        try:
            native_result = self._inner.edit(str(path), text)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in edit(): {e}") from e
        return SyncResult.model_validate(native_result)

    def edit_many(self, edits: dict[str, str]) -> SyncResult:
        """Overlay many files atomically (one publish, one revision)."""
        self._check_open()
        self._invalidate_head_snap()
        try:
            native_result = self._inner.edit_many(edits)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in edit_many(): {e}") from e
        return SyncResult.model_validate(native_result)

    def edit_virtual(self, uri: str, text: str) -> SyncResult:
        """Overlay a virtual/unsaved buffer (e.g. "untitled:1").
        No disk involvement."""
        self._check_open()
        self._invalidate_head_snap()
        try:
            native_result = self._inner.edit_virtual(uri, text)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in edit_virtual(): {e}") from e
        return SyncResult.model_validate(native_result)

    def sync_path(self, path: str | StdPath) -> SyncResult:
        """Ingest a disk change for ``path``: drop any overlay and re-read
        disk."""
        self._check_open()
        self._invalidate_head_snap()
        try:
            native_result = self._inner.sync_path(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in sync_path(): {e}") from e
        return SyncResult.model_validate(native_result)

    def discard(self, path: str | StdPath) -> SyncResult:
        """Drop the overlay buffer for ``path``, reverting to disk."""
        self._check_open()
        self._invalidate_head_snap()
        try:
            native_result = self._inner.discard(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in discard(): {e}") from e
        return SyncResult.model_validate(native_result)

    def sync_all(self) -> SyncResult:
        """Rescan everything (in-place). Existing overlay buffers are
        preserved; ty re-walks and re-reads all files."""
        self._check_open()
        self._invalidate_head_snap()
        try:
            native_result = self._inner.sync_all()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in sync_all(): {e}") from e
        return SyncResult.model_validate(native_result)

    # ── File watching (Phase 8) ────────────────────────────────────

    def watch(self) -> None:
        """Start observing the filesystem for changes under the project's
        watched paths. Observed changes are debounced by ty and queued; call
        :meth:`poll_changes` to fold them into HEAD. Idempotent: a second
        call replaces the watcher."""
        self._check_open()
        try:
            self._inner.watch()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in watch(): {e}") from e

    def unwatch(self) -> None:
        """Stop observing the filesystem. Pending unpolled events are
        discarded."""
        self._check_open()
        try:
            self._inner.unwatch()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in unwatch(): {e}") from e

    def flush_watch(self) -> None:
        """Prompt the watcher to emit any debounced batch now. Still
        asynchronous; follow with a short poll loop in tests."""
        self._check_open()
        try:
            self._inner.flush_watch()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in flush_watch(): {e}") from e

    def poll_changes(self) -> SyncResult | None:
        """Drain every change the watcher has observed and fold it into HEAD
        as one revision. Returns the SyncResult, or None if nothing was
        pending (or every event was for a path with a live overlay buffer,
        which the buffer wins).

        Like the explicit write methods, this advances the revision and
        updates the live HEAD graph (if materialised)."""
        self._check_open()
        self._invalidate_head_snap()
        try:
            native_result = self._inner.poll_changes()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in poll_changes(): {e}") from e

        if native_result is None:
            return None
        result = SyncResult.model_validate(native_result)
        self._apply_graph_delta(result)
        return result

    def _inject_changes(self, changes: list[tuple[str, str]]) -> None:
        """Test seam: enqueue events as if the watcher observed them."""
        self._check_open()
        try:
            self._inner._inject_changes(changes)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in _inject_changes(): {e}") from e

    def _apply_graph_delta(self, result: SyncResult) -> None:
        """No-op stub — graph delta application wired in Phase 6/7."""
        pass

    # ── Lifecycle ────────────────────────────────────────────────────

    def reload(self) -> None:
        """Reload the project, clearing cached diagnostics and re-scanning."""
        self._check_open()
        self._invalidate_head_snap()
        try:
            self._inner.reload()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in reload(): {e}") from e

    def close(self) -> None:
        """Close the project and free Rust-side resources.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._closed:
            return
        self._invalidate_head_snap()
        self._inner.close()
        self._closed = True

    def __enter__(self) -> TyO3Session:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        # Guard against interpreter shutdown — if the native module is already
        # unloaded, self._inner will be None and we must not call into Rust.
        if getattr(self, "_inner", None) is None:
            return
        if not getattr(self, "_closed", True):
            warnings.warn(
                "TyO3Session was not closed explicitly. Use 'with TyO3Session(...)' or call session.close().",
                ResourceWarning,
                stacklevel=2,
            )
            try:
                self.close()
            except Exception:
                pass


# ── Snapshot ────────────────────────────────────────────────────────────────


class Snapshot(_ReadOps):
    """Immutable, revision-pinned, thread-shareable read view of a project.

    Created via :meth:`TyO3Session.snapshot`. Exposes every read method a
    session does, but no ``reload()``. All reads see the project exactly as it
    was when the snapshot was taken, regardless of later session reloads, and a
    single snapshot is safe to share across threads.
    """

    def __init__(self, native_snapshot: Any) -> None:
        self._inner = native_snapshot
        self._closed = False

    def _native(self) -> Any:
        self._check_open()
        return self._inner

    @property
    def revision(self) -> int:
        """The revision this snapshot is pinned to."""
        self._check_open()
        return self._inner.revision

    def close(self) -> None:
        """Release the pinned revision. Safe to call multiple times."""
        if self._closed:
            return
        self._inner.close()
        self._closed = True

    def __enter__(self) -> Snapshot:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_inner", None) is None:
            return
        if not getattr(self, "_closed", True):
            warnings.warn(
                "Snapshot was not closed explicitly. Use 'with session.snapshot()' or call snapshot.close().",
                ResourceWarning,
                stacklevel=2,
            )
            try:
                self.close()
            except Exception:
                pass


# ── LatestView — floating warm reads (Phase 9) ────────────────────────────


class LatestView(_ReadOps):
    """A floating, warm read view of the live HEAD.

    Every read reflects the *newest* HEAD revision (including edits made after
    this view was obtained) and reuses the type-checker's warm memos, so it is
    faster than a cold snapshot for "what is the current state?" glances.
    Internally each read is retried if a concurrent write cancels it, so
    callers never see a cancellation.

    Tradeoff (architecture §7): because a floating read shares HEAD's storage,
    it can briefly delay a concurrent write (until the read notices the write
    and retries). For isolated, repeatable reads that never perturb the
    writer, use :meth:`TyO3Session.snapshot` instead — distinct by intent.
    """

    def __init__(self, native_head_view: Any) -> None:
        self._inner = native_head_view
        self._closed = False

    def _native(self) -> Any:
        self._check_open()
        return self._inner

    # No close()/revision: a LatestView pins nothing and has no fixed revision
    # (it floats). It is valid as long as the owning session is open.
