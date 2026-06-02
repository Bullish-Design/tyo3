"""Python wrapper around the Rust PyTyProject extension.

Wraps ``tyo3.TyProject`` (the PyO3 class) with JSON parsing and Pydantic
model validation.  Each method:

1. Calls the corresponding Rust method (which returns JSON or a native list)
2. Parses the result
3. Converts string paths into :class:`tyo3.models.core.Path` models
4. Returns Pydantic-validated domain objects

Usage::

    from tyo3.rust_project import RustProject

    rp = RustProject("/path/to/project")
    files = rp.files()
    result = rp.check()

See RUST_BACKEND_IMPLEMENTATION.md §6.1 for the design.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path as StdPath
from pathlib import PurePosixPath

from tyo3.exceptions import (
    AnalysisError,
    InternalTyError,
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
)
from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)
from tyo3.models.analysis import (
    FileRange as ModelFileRange,
)
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverContent,
    HoverContentKind,
    HoverResult,
    Reference,
    ReferenceKind,
)
from tyo3.models.symbols import Symbol, SymbolKind

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


# ── Helpers ────────────────────────────────────────────────────────────────


def _string_path_to_typath(path_str: str) -> PurePosixPath:
    """Convert a file-system path string into a :class:`PurePosixPath`."""
    return PurePosixPath(path_str)


def _json_position_to_model(pos_data: dict) -> Position:
    """Convert a ``PositionDto`` dict into a :class:`Position` model."""
    return Position(line=pos_data["line"], column=pos_data["column"])


def _json_range_to_model(range_data: dict) -> Range:
    """Convert a ``RangeDto`` dict into a :class:`Range` model."""
    return Range(
        start=_json_position_to_model(range_data["start"]),
        end=_json_position_to_model(range_data["end"]),
    )


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

    # ── Files ────────────────────────────────────────────────────────

    def files(self) -> list[PurePosixPath]:
        """Return the file paths known to this project."""
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
        try:
            raw_json: str = self._inner.check()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativeAnalysisError as e:
            raise AnalysisError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in check(): {e}") from e

        data = json.loads(raw_json)
        diagnostics: list[Diagnostic] = []

        for d in data.get("diagnostics", []):
            range_ref = None
            if d.get("range"):
                range_ref = _json_range_to_model(d["range"])

            diagnostics.append(
                Diagnostic(
                    file=None,  # path-only for now
                    range=range_ref,
                    severity=DiagnosticSeverity(d.get("severity", "error")),
                    code=d.get("code"),
                    message=d.get("message", ""),
                    details=d.get("details", []),
                )
            )

        return CheckResult(
            diagnostics=diagnostics,
            files_checked=data.get("files_checked"),
            elapsed_ms=data.get("elapsed_ms"),
        )

    # ── Document Symbols ─────────────────────────────────────────────

    def document_symbols(self, path: str | StdPath) -> list[Symbol]:
        """Return symbols defined in the given file."""
        try:
            raw_json: str = self._inner.document_symbols(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePathError as e:
            raise PathResolutionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in document_symbols(): {e}") from e

        return [self._parse_symbol(s) for s in json.loads(raw_json)]

    # ── Workspace Symbols ────────────────────────────────────────────

    def workspace_symbols(self, query: str) -> list[Symbol]:
        """Search for symbols matching *query* across the project."""
        try:
            raw_json: str = self._inner.workspace_symbols(query)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in workspace_symbols(): {e}") from e

        return [self._parse_symbol(s) for s in json.loads(raw_json)]

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
        try:
            raw_json: str = getattr(self._inner, method)(str(path), line, column)
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

        return [self._parse_definition_target(t) for t in json.loads(raw_json)]

    # ── Find References ─────────────────────────────────────────────

    def find_references(
        self,
        path: str | StdPath,
        line: int,
        column: int,
        include_declaration: bool = True,
    ) -> list[Reference]:
        """Find all references to the symbol at *(line, column)*."""
        try:
            raw_json: str = self._inner.find_references(str(path), line, column, include_declaration)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in find_references(): {e}") from e

        data = json.loads(raw_json)
        refs: list[Reference] = []
        for r in data:
            refs.append(
                Reference(
                    path=_string_path_to_typath(r["path"]),
                    range=_json_range_to_model(r["range"]),
                    kind=ReferenceKind(r.get("kind", "other")),
                )
            )
        return refs

    # ── Hover ────────────────────────────────────────────────────────

    def hover(self, path: str | StdPath, line: int, column: int) -> HoverResult | None:
        """Get hover information for the symbol at *(line, column)*.

        Returns ``None`` when no hover information is available.
        """
        try:
            raw_json: str | None = self._inner.hover(str(path), line, column)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativePositionError as e:
            raise PositionError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in hover(): {e}") from e

        if raw_json is None:
            return None

        data = json.loads(raw_json)
        loc_data = data["location"]
        location = ModelFileRange(
            path=_string_path_to_typath(loc_data["path"]),
            range=_json_range_to_model(loc_data["range"]),
        )

        contents: list[HoverContent] = []
        for c in data.get("contents", []):
            kind_str = c.get("kind", "plain_text")
            try:
                kind = HoverContentKind(kind_str)
            except ValueError:
                kind = HoverContentKind.PLAIN_TEXT
            contents.append(HoverContent(kind=kind, value=c.get("value", "")))

        return HoverResult(location=location, contents=contents)

    # ── JSON parsing helpers ─────────────────────────────────────────

    def _parse_symbol(self, s: dict) -> Symbol:
        """Parse a SymbolDto JSON dict into a Symbol model."""
        location_data = s["location"]
        loc = ModelFileRange(
            path=_string_path_to_typath(location_data["path"]),
            range=_json_range_to_model(location_data["range"]),
        )
        sel_range = _json_range_to_model(s["selection_range"]) if s.get("selection_range") else None
        return Symbol(
            name=s["name"],
            qualified_name=s.get("qualified_name"),
            kind=SymbolKind(s.get("kind", "unknown")),
            location=loc,
            selection_range=sel_range,
            container_name=s.get("container_name"),
            deprecated=s.get("deprecated", False),
        )

    def _parse_definition_target(self, t: dict) -> DefinitionTarget:
        """Parse a DefinitionTargetDto JSON dict into a DefinitionTarget model."""
        sel_range = _json_range_to_model(t["selection_range"]) if t.get("selection_range") else None
        symbol = self._parse_symbol(t["symbol"]) if t.get("symbol") else None

        return DefinitionTarget(
            path=_string_path_to_typath(t["path"]),
            range=_json_range_to_model(t["range"]),
            selection_range=sel_range,
            symbol=symbol,
            module_name=t.get("module_name"),
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    def reload(self) -> None:
        """Reload the project, clearing cached diagnostics and re-scanning."""
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
