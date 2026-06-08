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

import json
import warnings
from pathlib import Path as StdPath
from pathlib import PurePosixPath
from typing import Any

from tyo3.exceptions import (
    ConfigError,
    FormatVersionError,
    InternalTyError,
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
    RevisionEvictedError,
    TyO3Error,
)
from tyo3.config import TyConfig
from tyo3.models.advanced import SemanticToken
from tyo3.models.analysis import CheckResult, Range
from tyo3.models.delta import CommitDelta
from tyo3.models.authored import AuthoredValue, AuthoredVersion
from tyo3.models.derived import DerivedValue
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
    from tyo3._native_impl import (
        RevisionEvictedError as _NativeRevisionEvictedError,
    )
    from tyo3._native_impl import (
        ConfigError as _NativeConfigError,
        FormatVersionError as _NativeFormatVersionError,
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

    class _NativeRevisionEvictedError(Exception):  # type: ignore[no-redef]
        pass

    class _NativeConfigError(Exception):  # type: ignore[no-redef]
        pass

    class _NativeFormatVersionError(Exception):  # type: ignore[no-redef]
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


class _OwnedView:
    """A convenience layer view (``session.code`` / ``session.layer(...)``) that
    **owns** the head snapshot it reads over.

    ``session.code`` / ``session.layer`` open a fresh head snapshot and build a
    *lazy* layer view (one that reads the snapshot on demand). This wrapper holds
    that snapshot pinned for the view's lifetime and closes it on context-manager
    exit, explicit :meth:`close`, or garbage collection — so the returned view is
    never backed by an already-closed snapshot (V1 §5.2, deviation #7). Views
    taken directly from a :class:`Snapshot` (``snap.code`` / ``snap.layer``) are
    unaffected: there the ``Snapshot`` owns its own lifetime.

    The wrapper deliberately sits *outside* the ``Snapshot``↔view reference cycle
    (a ``Snapshot`` caches its views and each view refers back to the
    ``Snapshot``), so it is reclaimed by reference counting and releases its
    pinned snapshot deterministically — no leak, no spurious ``ResourceWarning``.
    It never closes the snapshot before handing the view back (the close-in-the-
    wrong-scope footgun this phase removes).
    """

    def __init__(self, snapshot: Any, view: Any) -> None:
        self._snapshot = snapshot
        self._view = view
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        # Reached only for attributes absent on the wrapper itself → delegate to
        # the wrapped view. Guard the private names so a partially constructed
        # wrapper raises ``AttributeError`` rather than recursing.
        if name in ("_snapshot", "_view", "_closed"):
            raise AttributeError(name)
        return getattr(self._view, name)

    def close(self) -> None:
        """Release the owned snapshot. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        self._snapshot.close()

    def __enter__(self) -> _OwnedView:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


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
        except _NativeFormatVersionError as e:
            raise FormatVersionError(str(e)) from e
        except _NativeConfigError as e:
            raise ConfigError(str(e)) from e
        except Exception as e:
            raise ProjectOpenError(f"Cannot open project at '{root_str}': {e}") from e
        self._config = TyConfig.from_json(self._inner.config_json())
        self._root = StdPath(root_str).resolve()
        self._closed = False
        self._head_snap: Any = None  # cached native head snapshot (current revision)
        self._head_graph: Any = None  # lazily-built mutable CodeGraph for HEAD
        self._derivation: Any = None  # lazily-built DerivationDAG
        self._bus: Any = None  # lazily-built Bus (Gate 8)
        self._watcher_thread: Any = None  # auto-poll daemon thread
        self._watcher_stop: Any = None  # threading.Event for watcher stop
        self._refiner: Any = None  # lazily-built PrecisionRefiner (Phase 9)
        # Affected-set precision policy (Concept V2 §5.4). Read from the one
        # validated native config (single source) — not a second TOML parser.
        cg = self._config.code_graph
        self._precision_mode: str = cg.precision
        self._refinement_mode: str = cg.refinement
        from tyo3.sidecar import Sidecar
        self._sidecar = Sidecar(str(root_str))
        # Coordination settings come from the one validated native config
        # (single source; no second TOML parser, no silent default fallback).
        self._coord_cfg = self._config.coordination
        # Auto-start watcher if configured (Step 7).
        if self._coord_cfg.watcher_enabled:
            self._start_watcher_loop()

    @property
    def root(self) -> StdPath:
        return self._root

    @property
    def config(self) -> TyConfig:
        return self._config

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
        lv = LatestView(native_head_view, session=self)
        return lv

    @property
    def graph(self):
        """The live HEAD graph — a pure projection of the native code delta.

        Built (and rebuilt) by applying ``full_code_delta()`` to a fresh
        ``CodeGraph``: no read-surface walk, no identity priming, no session
        write. Reading it never advances ``head`` (§5.3 / §5.9).
        """
        self._check_open()
        if self._head_graph is None:
            self._rebuild_head_graph_from_native()
        return self._head_graph

    def _rebuild_head_graph_from_native(self) -> None:
        """(Re)build the live HEAD graph from a full native code delta.

        A pure projection: apply ``full_code_delta()`` (a full/``rescan`` delta
        over the current head state) to the live HEAD ``CodeGraph``. A rescan
        delta clears-and-rebuilds the graph **in place**, so the head-graph
        instance is stable across commits (callers may hold a reference to it);
        a fresh ``CodeGraph`` is allocated only on first materialisation. Shared
        by the lazy ``graph`` property and the post-commit rebuild branch (the
        deferred-producer path). Mutates no native state — ``full_code_delta()``
        is a pure read of the head.
        """
        from tyo3.graph import CodeGraph

        g = self._head_graph
        if g is None:
            g = CodeGraph()
            g._root = self._root
            self._head_graph = g
        g.apply_code_delta(self._inner.full_code_delta())
        # Read-only diagnostics refresh (check() is a read, never sync_all).
        g.refresh_diagnostics(self, root=self._root)

    def _head_graph_or_none(self) -> Any:
        """Return the live HEAD graph if materialized; never build it."""
        return self._head_graph

    def _get_derivation(self):
        """Lazily build the DerivationDAG from session config."""
        if self._derivation is None:
            from tyo3.derive.dag import DerivationDAG
            self._derivation = DerivationDAG.from_session(self)
        return self._derivation

    def _invalidate_derived(self, result: CommitDelta) -> None:
        """Drive derived invalidation from the id-level delta (§5.5, Phase 8).

        Feeds the loop **durable ids**, never the path-shaped metadata. The
        candidate set is the transitive, container-granular ``affected_ids``
        closure (which already subsumes ``changed`` ∪ ``created`` ∪ the
        reverse-dependents); ``created``/``changed`` are unioned in defensively
        so a delta that populates only those still invalidates. Deletions drop
        by durable id.
        """
        dag = self._get_derivation()
        if dag.is_empty:
            return
        dirty = (
            set(result.affected_ids)
            | set(result.changed_ids)
            | set(result.created_ids)
        )
        dag.invalidate(self, dirty, set(result.deleted_ids), result.revision)

    # ── Watcher lifecycle (Gate 8 Step 7) ──────────────────────────

    def _start_watcher_loop(self) -> None:
        """Start the file watcher and a daemon auto-poll thread.

        Called automatically if ``[coordination.watcher].enabled = true``.
        Idempotent — a second call stops the previous loop first.
        """
        import threading

        # Stop any existing loop.
        self._stop_watcher_loop()

        # Start the native watcher.
        self.watch()

        # Start the auto-poll daemon thread.
        self._watcher_stop = threading.Event()
        debounce = self._coord_cfg.watcher_debounce_ms / 1000.0

        def _poll_loop() -> None:
            while not self._watcher_stop.is_set():
                # Wait with interruptible sleep.
                if self._watcher_stop.wait(timeout=debounce):
                    break
                if self._closed:
                    break
                try:
                    self.poll_changes()
                    # poll_changes funnels through the one _after_commit hook
                    # (applies the graph delta + publishes) on a real commit.
                except Exception:
                    pass

        self._watcher_thread = threading.Thread(
            target=_poll_loop, daemon=True, name="tyo3-watcher"
        )
        self._watcher_thread.start()

    def _stop_watcher_loop(self) -> None:
        """Stop the watcher auto-poll daemon thread (Step 7).

        No-op if the watcher was never started or the attributes
        don't exist (session constructed without __init__).
        """
        wt = getattr(self, "_watcher_thread", None)
        ws = getattr(self, "_watcher_stop", None)
        if wt is not None:
            if ws is not None:
                ws.set()
            if wt.is_alive():
                wt.join(timeout=2.0)
            self._watcher_thread = None
            self._watcher_stop = None
        # Also stop the native watcher.
        try:
            self.unwatch()
        except Exception:
            pass

    # ── Subscription bus (Gate 8) ──────────────────────────────────

    def _get_bus(self):
        """Lazily build the Bus from coordination config."""
        if self._bus is None:
            from tyo3.bus.bus import Bus
            self._bus = Bus(
                capacity=self._coord_cfg.bus_capacity,
                overflow=self._coord_cfg.bus_overflow,
            )
        return self._bus

    def subscribe(self, interest):
        """Register a subscriber on the delta subscription bus.

        Returns a ``Subscription`` that receives scoped deltas for
        every committed revision whose transitive affected set
        intersects *interest* (§12.2.1).
        """
        self._check_open()
        return self._get_bus().subscribe(interest)

    def _publish_delta(self, result: CommitDelta) -> None:
        """Publish a committed delta to the bus (the last step of the one
        post-commit hook, ``_after_commit``).

        Single-threaded writes serialised through the native commit guarantee
        revision order. Fast no-op when no subscribers are registered.
        """
        bus = self._bus
        if bus is None or not bus.has_subscribers():
            return
        from tyo3.bus.delta import Delta
        # The bus delta is a *pure projection* of the commit delta (§5.11): every
        # field — including the transitive ``affected`` closure and its
        # project-relative ``affected_files`` — is emitted natively by the
        # in-commit producer (Phase 6). The bus no longer depends on the
        # materialised head graph (Phase 7 deleted the Option-B bridge).
        delta = Delta.from_commit_delta(result)
        bus.publish(delta)

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
        except _NativeRevisionEvictedError as e:
            raise RevisionEvictedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in snapshot(): {e}") from e
        return Snapshot(
            native_snapshot,
            root=self._root,
            config=self._config,
            head_graph_getter=self._head_graph_or_none,
            derivation_getter=self._get_derivation,
        )

    # ── Write path ────────────────────────────────────────────────────

    def edit(self, path: str | StdPath, text: str) -> CommitDelta:
        """Overlay ``path`` with in-memory ``text`` (no disk write).
        Returns a SyncResult with the new revision and affected paths."""
        self._check_open()
        try:
            native_result = self._inner.edit(str(path), text)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in edit(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    def edit_many(self, edits: dict[str, str]) -> CommitDelta:
        """Overlay many files atomically (one publish, one revision)."""
        self._check_open()
        try:
            native_result = self._inner.edit_many(edits)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in edit_many(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    def edit_virtual(self, uri: str, text: str) -> CommitDelta:
        """Overlay a virtual/unsaved buffer (e.g. "untitled:1").
        No disk involvement."""
        self._check_open()
        try:
            native_result = self._inner.edit_virtual(uri, text)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in edit_virtual(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    def sync_path(self, path: str | StdPath) -> CommitDelta:
        """Ingest a disk change for ``path``: drop any overlay and re-read
        disk."""
        self._check_open()
        try:
            native_result = self._inner.sync_path(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in sync_path(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    def discard(self, path: str | StdPath) -> CommitDelta:
        """Drop the overlay buffer for ``path``, reverting to disk."""
        self._check_open()
        try:
            native_result = self._inner.discard(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in discard(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        # Routes through the one post-commit hook — so discard now publishes
        # like every other write (closes defect #6).
        self._after_commit(result)
        return result

    def sync_all(self) -> CommitDelta:
        """Rescan everything (in-place). Existing overlay buffers are
        preserved; ty re-walks and re-reads all files."""
        self._check_open()
        try:
            native_result = self._inner.sync_all()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in sync_all(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

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

    def poll_changes(self) -> CommitDelta | None:
        """Drain every change the watcher has observed and fold it into HEAD
        as one revision. Returns the SyncResult, or None if nothing was
        pending (or every event was for a path with a live overlay buffer,
        which the buffer wins).

        Like the explicit write methods, this advances the revision and
        updates the live HEAD graph (if materialised)."""
        self._check_open()
        try:
            native_result = self._inner.poll_changes()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in poll_changes(): {e}") from e

        # A no-event poll commits nothing — return None and publish nothing
        # (correct, not a missed publish). The hook runs only on a real commit.
        if native_result is None:
            return None
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
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

    def _apply_graph_delta(self, result: CommitDelta) -> None:
        """Update the materialized HEAD graph from the native code delta (§5.3).

        The pure graph applier (Phase 4); derived invalidation is a separate
        post-commit step (``_schedule_derived``), not done here.

        An **authored-only** write (only ``authored_ids``, no code churn)
        touches no code/edge structure, so this is a no-op for it — that is what
        lets ``author`` route through the one ``_after_commit`` hook without
        mutating the code graph (§6.2).

        Three-state on ``result.code_delta`` (Phase 4):
          * ``None`` (absent) — no structural delta was computed this commit ⇒
            **rebuild** the head graph from a full native delta. This is the
            deferred-producer path and is taken on every materialised-graph
            code commit today.
          * present, empty — computed, nothing changed structurally (e.g. a
            whitespace-only edit) ⇒ a clean **no-op** apply.
          * present, populated — the incremental delta ⇒ **apply** it,
            revision-gated.
        """
        if self._head_graph is None:
            return
        # An authored write carries no code structure — never mutate the graph
        # (its code_delta is None like the deferred-producer rebuild path, so it
        # must be discriminated by its id shape, not by code_delta).
        if self._is_authored_only(result):
            return
        code_delta = result.code_delta
        if code_delta is None:
            self._rebuild_head_graph_from_native()
            return
        # Present delta — incremental apply, revision-gated. A revision *gap*
        # (the delta skips revisions) can't be applied incrementally, so rebuild
        # from a fresh full delta; the in-order case applies (a stale delta is an
        # internal no-op inside apply_code_delta).
        cur = self._head_graph.revision
        new_rev = code_delta.get("revision")
        if cur is not None and new_rev is not None and new_rev > cur + 1:
            self._rebuild_head_graph_from_native()
        else:
            self._head_graph.apply_code_delta(code_delta)

    @staticmethod
    def _is_authored_only(delta: CommitDelta) -> bool:
        """True for a pure authored write: it carries ``authored_ids`` but no
        code churn (no created/changed/deleted/moved ids and no rescan), so it
        must not mutate the code graph."""
        return bool(delta.authored_ids) and not (
            delta.created_ids
            or delta.changed_ids
            or delta.deleted_ids
            or delta.moved
            or delta.rescan
        )

    def _schedule_derived(self, delta: CommitDelta) -> None:
        """Schedule derived-layer invalidation from the id-level delta (§5.7).

        Phase 6 only *calls* the existing invalidation from the one post-commit
        hook; Phase 7 makes it precise (read-time staleness, snapshot lifetime).
        An authored-only write produces no dirty/deleted ids, so this is a
        no-op for it.
        """
        self._invalidate_derived(delta)

    def _after_commit(self, delta: CommitDelta) -> None:
        """The single post-commit path every write funnels through (§6.1/§6.3).

        Invalidate the head snapshot (so the next read re-pins at the new
        revision), apply the native code delta to the head graph, schedule
        derived invalidation, then publish to the bus — in that order. Because
        every write method calls exactly this, no path can diverge and every
        committed revision publishes (closes defect #6: ``discard`` forgetting
        to publish).
        """
        self._invalidate_head_snap()
        self._apply_graph_delta(delta)
        self._schedule_derived(delta)
        self._publish_delta(delta)
        # Async precision refinement (Phase 9) — strictly *after* primary
        # delivery, so the coarse set is always delivered first and the writer
        # never waits on precision. A no-op unless precision=method.
        self._maybe_refine(delta)

    def _get_refiner(self):
        """Lazily build the PrecisionRefiner (Phase 9, precision=method)."""
        if self._refiner is None:
            from tyo3.precision import PrecisionRefiner
            self._refiner = PrecisionRefiner(self, mode=self._refinement_mode)
        return self._refiner

    def _maybe_refine(self, delta: CommitDelta) -> None:
        """Feed the precision refiner when precision=method (§5.4).

        Defaults to ``container`` ⇒ the refiner is never built and the writer
        pays nothing. With ``method`` the coarse delta is handed to the refiner
        (async: enqueued; sync: computed inline) which narrows it and publishes
        an ``AffectedRefinement`` on the Phase-7 refinement channel.
        """
        if self._precision_mode != "method":
            return
        # Nothing to refine without a bus to publish to (and no subscribers
        # means a no-op publish anyway) — skip building the worker.
        bus = self._bus
        if bus is None or not bus.has_subscribers():
            return
        try:
            self._get_refiner().feed(
                delta.revision, delta.changed_ids, delta.affected_ids
            )
        except Exception:
            # Never let precision wiring surface as a write-path failure — the
            # coarse set was already published (graceful degradation).
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
        self._head_graph = None

    # ── Identity (Gate 2) ────────────────────────────────────────────

    def id_for(self, path: str, line: int, col: int) -> Optional[str]:
        """Resolve the DurableId of the entity at (path, line, col).

        Returns None if no entity was found or no identity is registered.
        """
        self._check_open()
        try:
            return self._inner.id_for(path, line, col)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in id_for(): {e}") from e

    def locate(self, durable_id: str) -> Optional[str]:
        """Locate the current file::qualified_path for a DurableId.

        Returns None if the id is not in the registry.
        """
        self._check_open()
        try:
            return self._inner.locate(durable_id)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in locate(): {e}") from e

    def needs_review(self) -> list[str]:
        """List durable ids currently flagged as NeedsReview."""
        self._check_open()
        try:
            return self._inner.needs_review()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in needs_review(): {e}") from e

    def orphaned(self) -> list[str]:
        """List durable ids currently flagged as Orphaned."""
        self._check_open()
        try:
            return self._inner.orphaned()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in orphaned(): {e}") from e

    def gc(self) -> None:
        """Explicit GC pass: evict orphaned derived artifacts (Step 9).

        Only layers with ``gc = "orphans"`` are affected; ``gc = "never"``
        (the default) keeps everything. GC is never run implicitly.
        """
        self._check_open()
        dag = self._get_derivation()
        if dag.is_empty:
            return
        dag.gc_orphans(self)

    # ── Derived (Gate 5) ───────────────────────────────────────────

    def derived(self, layer: str, durable_id: str) -> Any:
        """Read a derived value from the HEAD snapshot.

        Delegates to the head snapshot's Python-level derived method,
        not the native handle (which doesn't expose derived directly).
        """
        self._check_open()
        snap = self.snapshot()
        try:
            return snap.derived(layer, durable_id)
        finally:
            snap.close()

    def nearest(
        self, query_vector: list[float], k: int = 10, *, layer: str | None = None
    ) -> list[tuple[str, float]]:
        """Nearest-neighbour search from the HEAD snapshot."""
        self._check_open()
        snap = self.snapshot()
        try:
            return snap.nearest(query_vector, k, layer=layer)
        finally:
            snap.close()

    def embedding(self, durable_id: str) -> Any:
        """Convenience: derived value from the 'embeddings' layer."""
        return self.derived("embeddings", durable_id)

    def docstring(self, durable_id: str) -> Any:
        """Convenience: derived value from the 'docstrings' layer."""
        return self.derived("docstrings", durable_id)

    # ── Authored (Gate 6) ───────────────────────────────────────────

    def author(self, layer: str, durable_id: str, value: Any) -> CommitDelta:
        """Author (write) an authored value for ``(layer, durable_id)``.

        An authored write is a real revision-producing commit: it bumps the
        revision, persists the record crash-safely, and returns a delta.
        Derived layers are unaffected — authored layers are sinks (§9.2.4).
        """
        self._check_open()
        payload = json.dumps(value)
        try:
            native = self._inner.author(layer, durable_id, payload)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in author(): {e}") from e
        result = CommitDelta.model_validate(native)
        # Routes through the one post-commit hook like every other write; its
        # graph apply is a no-op (authored-only delta), so the code graph is
        # not mutated (§6.2).
        self._after_commit(result)
        return result

    def authored(self, layer: str, durable_id: str) -> AuthoredValue:
        """Read an authored value from the HEAD snapshot."""
        self._check_open()
        native = self._native()
        try:
            dto = native.authored(layer, durable_id)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in authored(): {e}") from e
        return AuthoredValue.model_validate(dto)

    def authored_needs_review(self, layer: str | None = None) -> list[str]:
        """List durable ids with authored records flagged `needs_review`.

        If `layer` is given, filters to that authored layer."""
        self._check_open()
        # Collect from most recent sync result (or derive from registry).
        # For the HEAD, we derive from the current state.
        ids = []
        for lname, lcfg in self.config.layers.items():
            if lcfg.origin != "authored":
                continue
            if layer is not None and lname != layer:
                continue
            if not lcfg.review_on_change:
                continue
            for nrid in self.needs_review():
                # Check if this id has an authored record in this layer.
                try:
                    val = self.authored(lname, nrid)
                    if val.status == "needs_review":
                        ids.append(nrid)
                except Exception:
                    pass
        return sorted(set(ids))

    def authored_orphaned(self, layer: str | None = None) -> list[str]:
        """List durable ids with authored records flagged `orphaned`.

        If `layer` is given, filters to that authored layer."""
        self._check_open()
        ids = []
        for lname, lcfg in self.config.layers.items():
            if lcfg.origin != "authored":
                continue
            if layer is not None and lname != layer:
                continue
            if not lcfg.review_on_change:
                continue
            for orph_id in self.orphaned():
                try:
                    val = self.authored(lname, orph_id)
                    if val.status == "orphaned":
                        ids.append(orph_id)
                except Exception:
                    pass
        return sorted(set(ids))

    # ── Convenience read sugar (owns / pins a fresh head snapshot) ──
    #
    # Each convenience read opens a fresh head snapshot. ``entity`` / ``diff``
    # build an eagerly-materialised frozen result and close the snapshot before
    # returning, so no lazy state escapes. ``code`` / ``layer`` return a *lazy*
    # layer view that reads the snapshot on demand, so the view owns the
    # snapshot (via :class:`_OwnedView`) and keeps it pinned for its lifetime.
    # No convenience read ever returns a view backed by an already-closed
    # snapshot (V1 §5.2 / deviation #7).

    @property
    def code(self):
        """A ``CodeLayerView`` over a fresh head snapshot the view **owns**.

        The returned view keeps its snapshot pinned for its lifetime — use it as
        a context manager (``with session.code as code: ...``), call ``close()``,
        or let GC release it. Reading it never advances ``head``.
        """
        self._check_open()
        snap = self.snapshot()
        try:
            return _OwnedView(snap, snap.code)
        except Exception:
            snap.close()
            raise

    def layer(self, name: str):
        """Dispatch to the right ``LayerView`` by *name* over an **owned** snapshot.

        The returned view owns its snapshot (see :meth:`code`). Raises
        ``KeyError`` if *name* is not a declared layer.
        """
        self._check_open()
        snap = self.snapshot()
        try:
            return _OwnedView(snap, snap.layer(name))
        except Exception:
            snap.close()
            raise

    def entity(self, durable_id: str):
        """An ``EntityView`` for *durable_id* at a fresh head snapshot.

        The cross-layer join is **eagerly materialised** against the snapshot
        before it is closed, so the returned frozen view holds no live snapshot
        state. Sugar for ``session.snapshot().entity(id)``.
        """
        self._check_open()
        with self.snapshot() as snap:
            return snap.entity(durable_id)

    def diff(self, before: Snapshot) -> Any:
        """A ``SnapshotDiff`` between *before* and a fresh head snapshot.

        ``SnapshotDiff.compute`` **eagerly materialises** every layer diff
        against both snapshots, so closing the after-snapshot before returning
        is safe. Sugar for ``Snapshot.diff(before)``.
        """
        self._check_open()
        with self.snapshot() as snap:
            return snap.diff(before)

    def close(self) -> None:
        """Close the project and free Rust-side resources.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._closed:
            return
        # Stop watcher auto-poll loop (Step 7).
        self._stop_watcher_loop()
        # Stop the precision refiner daemon (Phase 9) before tearing the bus
        # down, so no in-flight refinement races a closing bus.
        refiner = getattr(self, "_refiner", None)
        if refiner is not None:
            try:
                refiner.stop()
            except Exception:
                pass
            self._refiner = None
        # Close the bus (closes all subscriptions).
        bus = getattr(self, "_bus", None)
        if bus is not None:
            bus.close()
            self._bus = None
        self._invalidate_head_snap()
        self._head_graph = None
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

    def __init__(
        self,
        native_snapshot: Any,
        *,
        root: StdPath,
        config: TyConfig | None = None,
        head_graph_getter: Any | None = None,
        derivation_getter: Any | None = None,
    ) -> None:
        self._inner = native_snapshot
        self._closed = False
        self._root = root
        self._config = config
        self._head_graph_getter = head_graph_getter
        self._derivation_getter = derivation_getter
        self._graph: Any = None
        self._code_view: Any = None
        self._layer_views: dict[str, Any] = {}

    def _native(self) -> Any:
        self._check_open()
        return self._inner

    @property
    def revision(self) -> int:
        """The revision this snapshot is pinned to."""
        self._check_open()
        return self._inner.revision

    @property
    def code(self):
        """A ``CodeLayerView`` pinned at this snapshot's revision."""
        if self._code_view is None:
            from tyo3.layers.code import CodeLayerView
            self._code_view = CodeLayerView(self)
        return self._code_view

    def layer(self, name: str):
        """Dispatch to the right ``LayerView`` by *name* via config origin.

        Raises ``KeyError`` if *name* is not a declared layer.
        """
        if name == "code":
            return self.code
        if name in self._layer_views:
            return self._layer_views[name]
        if self._config is None:
            raise KeyError(f"No config available — cannot resolve layer '{name}'")
        layer_cfg = self._config.layers.get(name)
        if layer_cfg is None:
            raise KeyError(f"Layer '{name}' is not declared in config")
        if layer_cfg.origin == "derived":
            from tyo3.layers.derived import DerivedLayerView
            view = DerivedLayerView(self, name, layer_cfg)
        elif layer_cfg.origin == "authored":
            from tyo3.layers.authored import AuthoredLayerView
            view = AuthoredLayerView(self, name, layer_cfg)
        else:
            raise KeyError(f"Layer '{name}' has unknown origin '{layer_cfg.origin}'")
        self._layer_views[name] = view
        return view

    def entity(self, durable_id: str):
        """Return an ``EntityView`` — the cross-layer join for *durable_id* at this revision.

        Members: ``code``, ``content_hash``, ``location``, ``status``,
        ``derived``, ``authored`` — all describing the pinned revision.
        """
        from tyo3.models.view import EntityView
        return EntityView.from_snapshot(self, durable_id)

    def embedding_drift(self, before: Snapshot) -> Any:
        """Convenience: derived drift from the 'embeddings' layer.

        Sugar for ``self.layer("embeddings").diff(before.layer("embeddings"))``.
        Raises ``KeyError`` if no embeddings layer is configured.
        """
        return self.layer("embeddings").diff(before.layer("embeddings"))

    def diff(self, before: Snapshot) -> Any:
        """Return a ``SnapshotDiff`` — the combined, id-keyed diff across all layers.

        *before* must be a snapshot from the same session; *self* is *after*.
        Raises ``ValueError`` when snapshots are from incompatible sessions.
        """
        from tyo3.models.diff import SnapshotDiff
        return SnapshotDiff.compute(self, before)

    def graph(self):
        """Return an immutable CodeGraph pinned at this snapshot's revision.

        Built by applying the snapshot's **own frozen-database** native code
        delta (4.4) to a fresh CodeGraph — no read-surface walk, no session
        reference, no identity priming. The result is consistent with the
        snapshot's pinned revision and mutates no session state.
        """
        self._check_open()
        if self._graph is not None:
            return self._graph

        # Fast path: if the live HEAD graph is already materialised at exactly
        # this revision, pin a copy of it (avoids recomputing the frozen delta).
        head_graph = self._head_graph_getter() if self._head_graph_getter is not None else None
        if head_graph is not None and head_graph.revision == self.revision:
            self._graph = head_graph._pin_at(self.revision)
            return self._graph

        from tyo3.graph import CodeGraph

        g = CodeGraph()
        g._root = self._root
        g.apply_code_delta(self._inner.full_code_delta())  # frozen-db delta (4.4)
        g.refresh_diagnostics(self, root=self._root)  # read-only (snapshot.check())
        self._graph = g._pin_at(self.revision)
        return self._graph

    def derived(self, layer: str, durable_id: str) -> DerivedValue:
        """Resolve a derived value for *durable_id* under *layer* at this revision.

        Content-addressed: the artifact is looked up by the entity's content
        hash at this revision, so the result is exact for R.

        Honest staleness (§8.2.5):
        - Fresh: artifact matches entity content at R.
        - Stale: last-good artifact served (default policy).
        - Failed: generator failed, prior artifact (if any) served.
        - Absent: no artifact ever produced.
        """
        self._check_open()
        if self._derivation_getter is None:
            return DerivedValue(
                artifact=None, status="absent", revision=self.revision, layer=layer
            )
        dag = self._derivation_getter()
        if dag.is_empty:
            return DerivedValue(
                artifact=None, status="absent", revision=self.revision, layer=layer
            )
        L = dag.layer(layer)
        try:
            gen_input, input_hash = dag.resolve_input(L, self, durable_id)
        except (KeyError, AttributeError):
            # The entity does not resolve at this revision (deleted, or never
            # reconciled to this snapshot) → nothing to serve.
            return DerivedValue(
                artifact=None, status="absent", revision=self.revision, layer=layer
            )
        key = L.keys_for(input_hash)
        art = L.cache.get(key)
        if art is not None:
            # Present at the resolved key ⇒ fresh. Read-time staleness is a pure
            # key comparison (§8.3): the content-addressed key already encodes
            # the entity content (and, for semantic layers, its dependency
            # closure), so a move-unchanged hit serves the reused artifact.
            return DerivedValue(artifact=art, status="fresh", revision=self.revision, layer=layer)

        # Miss at the resolved key. Self-heal with a synchronous recompute over
        # this pinned snapshot (§8.3). With no async worker in Phase 8 this
        # serves both `block` and `stale` layers; the serving policy is honoured
        # on failure, where we fall back to the last-good artifact tagged
        # honestly. The async refinement path is Phase 9.
        scheduler = dag._get_scheduler()
        art = scheduler.recompute_now(dag, L, self, durable_id)
        if art is not None:
            return DerivedValue(artifact=art, status="fresh", revision=self.revision, layer=layer)

        # Recompute failed (or produced nothing) → serve last-good honestly.
        last_key = L.last_good_store_key(durable_id)
        if last_key:
            last_art = L.cache._store.get(last_key)
            if last_art is not None:
                status = "failed" if durable_id in L._failed else "stale"
                return DerivedValue(artifact=last_art, status=status, revision=self.revision, layer=layer)
        # No last-good. A recorded generator failure is reported as `failed`
        # (artifact None); a genuine never-produced artifact is `absent`.
        if durable_id in L._failed:
            return DerivedValue(artifact=None, status="failed", revision=self.revision, layer=layer)
        return DerivedValue(artifact=None, status="absent", revision=self.revision, layer=layer)

    def nearest(
        self, query_vector: list[float], k: int = 10, *, layer: str | None = None
    ) -> list[tuple[str, float]]:
        """Nearest-neighbour search against a vector-backed derived layer.

        Returns list of ``(durable_id, score)`` for entities with the nearest
        vectors at this revision.
        """
        self._check_open()
        if self._derivation_getter is None:
            return []
        dag = self._derivation_getter()
        if dag.is_empty:
            return []

        # Default to the first vector-backed layer.
        if layer is None:
            for name in dag._topo_order:
                if name in dag._layer_map:
                    L = dag._layer_map[name]
                    if hasattr(L.cache._store, "nearest"):
                        layer = name
                        break
        if layer is None:
            return []

        L = dag.layer(layer)
        store = L.cache._store
        if not hasattr(store, "nearest"):
            return []

        # Query the vector backend.
        results: list[tuple[str, float]] = store.nearest(query_vector, k)  # type: ignore[union-attr]
        if not results:
            return []

        # Map input_hash keys back to DurableIds at this revision.
        g = self.graph()
        hash_to_ids: dict[str, list[str]] = {}
        for node_idx in g._graph.node_indices():
            node = g._graph[node_idx]
            for profile, h in node.content_hashes.items():
                hash_to_ids.setdefault(h, []).append(node.durable_id)

        mapped: list[tuple[str, float]] = []
        seen: set[str] = set()
        for store_key, score in results:
            # Store key is "input_hash:version" — extract input_hash.
            input_hash = store_key.split(":")[0] if ":" in store_key else store_key
            ids = hash_to_ids.get(input_hash, [])
            for did in ids:
                if did not in seen:
                    seen.add(did)
                    mapped.append((did, score))

        return mapped

    def embedding(self, durable_id: str) -> DerivedValue:
        """Convenience: derived value from the 'embeddings' layer."""
        return self.derived("embeddings", durable_id)

    def docstring(self, durable_id: str) -> DerivedValue:
        """Convenience: derived value from the 'docstrings' layer."""
        return self.derived("docstrings", durable_id)

    def authored(self, layer: str, durable_id: str) -> AuthoredValue:
        """Resolve an authored value at this snapshot's pinned revision.

        Status is honest (§10.2.3): absent, present, needs_review, or orphaned —
        derived from the snapshot-captured identity registry.
        """
        self._check_open()
        dto = self._inner.authored(layer, durable_id)
        return AuthoredValue.model_validate(dto)

    def authored_history(self, layer: str, durable_id: str) -> list[AuthoredVersion]:
        """Return the full version history for an authored record.

        Returns all versions (history + current) with revision ≤ this
        snapshot's pinned revision, ordered by revision ascending.
        Empty list if the record doesn't exist.
        """
        self._check_open()
        dtos = self._inner.authored_history(layer, durable_id)
        return [AuthoredVersion.model_validate(d) for d in dtos]

    def close(self) -> None:
        """Release the pinned revision. Safe to call multiple times."""
        if self._closed:
            return
        self._inner.close()
        self._graph = None
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

    Honest boundary: cross-layer consistency and diff require a pinned
    revision, so ``entity()`` and ``diff()`` are **not** available here.
    Use :meth:`TyO3Session.snapshot` for consistent multi-layer joins.
    """

    def __init__(self, native_head_view: Any, session: Any = None) -> None:
        self._inner = native_head_view
        self._closed = False
        self._session = session  # weak reference for derived/authored warm reads

    def _native(self) -> Any:
        self._check_open()
        return self._inner

    def graph(self):
        """A **non-canonical, floating** code-graph projection of the live HEAD.

        Returns an *immutable, point-in-time copy* pinned at the current head
        revision — deliberately **not** the canonical mutable HEAD graph. Each
        call re-pins the newest head (the floating contract), and the returned
        graph never mutates under the caller. A floating view must not hand out a
        mutable reference to the canonical graph (Phase 11.2); for the canonical
        maintained head graph use ``session.graph``, and for a consistent pinned
        graph use ``session.snapshot().graph()``. Reading it never advances head.
        """
        self._check_open()
        if self._session is None:
            raise InternalTyError("No session reference for latest graph")
        head_graph = self._session.graph
        return head_graph._pin_at(self._session.head)

    def derived(self, layer: str, durable_id: str) -> Any:
        """Warm derived read against the live HEAD. Cancellation-retried.

        Resolves through the session's head snapshot (consistent, not
        floating) — each call is internally consistent but two successive
        calls may straddle a write.
        """
        self._check_open()
        if self._session is not None:
            return self._session.derived(layer, durable_id)
        raise InternalTyError("No session reference for latest derived read")

    def authored(self, layer: str, durable_id: str) -> Any:
        """Warm authored read against the live HEAD. Cancellation-retried."""
        self._check_open()
        if self._session is not None:
            return self._session.authored(layer, durable_id)
        raise InternalTyError("No session reference for latest authored read")

    # No close()/revision/entity/diff: a LatestView pins nothing and has no
    # fixed revision (it floats). It is valid as long as the owning session
    # is open. entity() and diff() are NOT exposed — the honest boundary.
