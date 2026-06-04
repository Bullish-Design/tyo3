"""Python wrapper around the Rust PyTyProject extension.

Wraps ``tyo3.TyProject`` (the PyO3 class) with Pydantic model validation.
Each Rust method returns native Python objects (dicts/lists via pythonize),
which are validated directly through Pydantic's ``model_validate``.

Usage::

    from tyo3.rust_project import RustProject

    rp = RustProject("/path/to/project")
    files = rp.files()
    result = rp.check()

See RUST_BACKEND_IMPLEMENTATION.md §6.1 for the design.
"""

from __future__ import annotations

import warnings
from pathlib import Path as StdPath
from pathlib import PurePosixPath

from tyo3.exceptions import (
    InternalTyError,
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
)
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


# ── RustProject ─────────────────────────────────────────────────────────────


class RustProject:
    """Wraps the Rust :class:`TyProject` for Pydantic-validated access.

    Owns an internal handle to the ``ProjectDatabase`` via the PyO3 boundary.
    All data returned by Rust methods is already-native Python (dicts/lists
    via pythonize) and validated directly through Pydantic's ``model_validate``.
    """

    def __init__(self, root: str | StdPath) -> None:
        if _native is None:
            raise ProjectOpenError(
                "Rust native extension is not built. Run `maturin develop` inside the devenv shell first."
            )
        root_str = str(root)
        try:
            self._inner = _native.TyProject.open(root_str)
        except Exception as e:
            raise ProjectOpenError(f"Cannot open project at '{root_str}': {e}") from e
        self._root = StdPath(root_str).resolve()
        self._closed = False

    @property
    def root(self) -> StdPath:
        return self._root

    # ── Guard ──────────────────────────────────────────────────────

    def _check_open(self) -> None:
        """Raise ProjectClosedError if this project has been closed."""
        if self._closed:
            raise ProjectClosedError("Project is closed")

    # ── Files ────────────────────────────────────────────────────────

    def files(self) -> list[PurePosixPath]:
        """Return the file paths known to this project."""
        self._check_open()
        try:
            raw: list[str] = self._inner.files()
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
            native_result = self._inner.check()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in check(): {e}") from e

        return CheckResult.model_validate(native_result)

    def check_file(self, path: str | StdPath) -> CheckResult:
        """Run the type-checker and return diagnostics for a single file."""
        self._check_open()
        try:
            native_result = self._inner.check_file(str(path))
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
            native_symbols = self._inner.document_symbols(str(path))
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
        try:
            native_symbols = self._inner.workspace_symbols(query)
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
            native_targets = getattr(self._inner, method)(str(path), line, column)
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
            native_refs = self._inner.find_references(str(path), line, column, include_declaration)
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

    # ── Semantic Tokens ──────────────────────────────────────────

    def semantic_tokens(self, path: str | StdPath) -> list[SemanticToken]:
        """Return semantic tokens for a file."""
        self._check_open()
        try:
            native_result = self._inner.semantic_tokens(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
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
            native_result = self._inner.file_occurrences(str(path))
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
            native_hover = self._inner.hover(str(path), line, column)
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
            native_result = self._inner.type_hierarchy(str(path), line, column)
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

    # ── Lifecycle ────────────────────────────────────────────────────

    def reload(self) -> None:
        """Reload the project, clearing cached diagnostics and re-scanning."""
        self._check_open()
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
        self._inner.close()
        self._closed = True

    def __enter__(self) -> RustProject:
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
                "RustProject was not closed explicitly. Use 'with RustProject(...) as rp:' or call rp.close().",
                ResourceWarning,
                stacklevel=2,
            )
            try:
                self.close()
            except Exception:
                pass
