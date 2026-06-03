"""Python wrapper around the Rust PyTyProject extension.

Wraps ``tyo3.TyProject`` (the PyO3 class) with Pydantic model validation.
Each method:

1. Calls the corresponding Rust method (which returns native PyO3 DTO objects)
2. Converts native objects to plain Python via :func:`_to_python`
3. Validates the result through Pydantic's ``model_validate``
4. Returns Pydantic-validated domain objects

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
from typing import Any

from tyo3.exceptions import (
    AnalysisError,
    InternalTyError,
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
)
from tyo3.models.analysis import CheckResult
from tyo3.models.navigation import DefinitionTarget, HoverResult, Reference
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
        AnalysisError as _NativeAnalysisError,
    )
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

    class _NativeAnalysisError(Exception):  # type: ignore[no-redef]
        pass


# ── Conversion helpers ────────────────────────────────────────────────────
# PyO3 enums (defined with #[pyclass(eq)]) are not Python str subclasses,
# so Pydantic StrEnum fields reject them.  PyO3 frozen structs lack
# __dict__, so Pydantic v2.13 from_attributes=True rejects them.
# We recursively convert the PyO3 object graph to plain Python types
# (dict, list, str) that Pydantic validates natively.


# Cache of known PyO3 enum types from the native extension.
# Built lazily on first use so the module can import without the .so.
_ENUM_TYPES: set[type] = set()
_ENUM_TYPES_BUILT: bool = False


def _build_enum_cache() -> None:
    """Cache the PyO3 enum types from the native extension module."""
    global _ENUM_TYPES, _ENUM_TYPES_BUILT
    if _ENUM_TYPES_BUILT or _native is None:
        return
    for name in dir(_native):
        if name.startswith("_"):
            continue
        # PyO3 enums have names ending in 'Kind' or are NativeSeverity
        if name.endswith("Kind") or name == "NativeSeverity":
            obj = getattr(_native, name)
            if isinstance(obj, type):
                _ENUM_TYPES.add(obj)
    _ENUM_TYPES_BUILT = True


def _is_native_enum(obj: Any) -> bool:
    """Return True if *obj* is a PyO3 native enum variant."""
    _build_enum_cache()
    return type(obj) in _ENUM_TYPES


def _to_python(obj: Any) -> Any:
    """Recursively convert PyO3 native objects to plain Python types.

    - PyO3 enums → ``str``
    - PyO3 frozen structs → ``dict`` with field-name keys
    - ``list`` → recursively converted list
    - ``tuple`` → recursively converted list
    - ``None``, ``str``, ``int``, ``float``, ``bool`` → passed through
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    if isinstance(obj, (list, tuple)):
        return [_to_python(item) for item in obj]

    if _is_native_enum(obj):
        return str(obj)

    # PyO3 frozen struct: convert to dict via #[pyo3(get)] fields
    result: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            val = getattr(obj, name)
            if not callable(val):
                result[name] = _to_python(val)
        except Exception:
            pass

    return result


# ── RustProject ─────────────────────────────────────────────────────────────


class RustProject:
    """Wraps the Rust :class:`TyProject` for Pydantic-validated access.

    Owns an internal handle to the ``ProjectDatabase`` via the PyO3 boundary.
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
        except _NativeAnalysisError as e:
            raise AnalysisError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in check(): {e}") from e

        return CheckResult.model_validate(_to_python(native_result))

    def check_file(self, path: str | StdPath) -> CheckResult:
        """Run the type-checker and return diagnostics for a single file."""
        self._check_open()
        try:
            native_result = self._inner.check_file(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except _NativeAnalysisError as e:
            raise AnalysisError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in check_file(): {e}") from e

        return CheckResult.model_validate(_to_python(native_result))

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

        return [Symbol.model_validate(_to_python(s)) for s in native_symbols]

    # ── Workspace Symbols ────────────────────────────────────────────

    def workspace_symbols(self, query: str) -> list[Symbol]:
        """Search for symbols matching *query* across the project."""
        if not query:
            return []
        self._check_open()
        try:
            native_symbols = self._inner.workspace_symbols(query)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in workspace_symbols(): {e}") from e

        return [Symbol.model_validate(_to_python(s)) for s in native_symbols]

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

        return [DefinitionTarget.model_validate(_to_python(t)) for t in native_targets]

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
        except Exception as e:
            raise InternalTyError(f"Unexpected error in find_references(): {e}") from e

        return [Reference.model_validate(_to_python(r)) for r in native_refs]

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
        except Exception as e:
            raise InternalTyError(f"Unexpected error in hover(): {e}") from e

        if native_hover is None:
            return None

        return HoverResult.model_validate(_to_python(native_hover))

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
                "RustProject was not closed explicitly. "
                "Use 'with RustProject(...) as rp:' or call rp.close().",
                ResourceWarning,
                stacklevel=2,
            )
            try:
                self.close()
            except Exception:
                pass
