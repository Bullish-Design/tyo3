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
from pathlib import Path as StdPath
from typing import Optional

from tyo3.exceptions import (
    AnalysisError,
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
)
from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    FileRange as ModelFileRange,
    Position,
    Range,
)
from datetime import datetime, timezone

from tyo3.models.core import Path as TyPath, TyProject as TyProjectModel
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverContent,
    HoverContentKind,
    HoverResult,
    Reference,
    ReferenceKind,
)
from tyo3.models.symbols import Symbol

# The PyO3 extension module — must match
# #[pyo3(name = "_native_impl")] in rust/src/lib.rs
# The .so is placed inside the tyo3 Python package at src/tyo3/_native_impl.cpython-*.so
try:
    from tyo3 import _native_impl as _native
except ImportError:
    _native = None  # type: ignore[assignment]


# ── Helpers ────────────────────────────────────────────────────────────────


def _string_path_to_typath(path_str: str) -> TyPath:
    """Convert a file-system path string into a :class:`TyPath` model."""
    p = StdPath(path_str)
    return TyPath(components=list(p.parts))


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
                "Rust native extension is not built. "
                "Run `maturin develop` inside the devenv shell first."
            )
        root_str = str(root)
        try:
            self._inner = _native.TyProject.open(root_str)
        except Exception as e:
            raise ProjectOpenError(f"Cannot open project at '{root_str}': {e}") from e
        self._root = StdPath(root_str).resolve()
        # Minimal TyProject model used to enrich DTOs that require a project reference.
        self._project_model = TyProjectModel(
            root=TyPath(components=list(self._root.parts)),
            status="open",
            opened_at=datetime.now(timezone.utc),
        )

    @property
    def root(self) -> StdPath:
        return self._root

    # ── Files ────────────────────────────────────────────────────────

    def files(self) -> list[str]:
        """Return the file paths known to this project."""
        try:
            return self._inner.files()
        except Exception as e:
            if "closed" in str(e).lower():
                raise ProjectClosedError(str(e)) from e
            raise

    # ── Check ────────────────────────────────────────────────────────

    def check(self) -> CheckResult:
        """Run the type-checker and return structured diagnostics."""
        try:
            raw_json: str = self._inner.check()
        except Exception as e:
            if "closed" in str(e).lower():
                raise ProjectClosedError(str(e)) from e
            raise AnalysisError(f"Check failed: {e}") from e

        data = json.loads(raw_json)
        diagnostics: list[Diagnostic] = []

        for d in data.get("diagnostics", []):
            range_ref = None
            if d.get("range"):
                range_ref = _json_range_to_model(d["range"])

            diagnostics.append(
                Diagnostic(
                    project=self._project_model,
                    file=None,  # path-only for now
                    range=range_ref,
                    severity=d.get("severity", "error"),
                    code=d.get("code"),
                    message=d.get("message", ""),
                    details=set(d.get("details", [])),
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
        except Exception as e:
            if "closed" in str(e).lower():
                raise ProjectClosedError(str(e)) from e
            if "resolve" in str(e).lower() or "not in project" in str(e).lower():
                raise PathResolutionError(str(e)) from e
            raise

        data = json.loads(raw_json)
        symbols: list[Symbol] = []
        for s in data:
            location_data = s["location"]
            loc = ModelFileRange(
                path=_string_path_to_typath(location_data["path"]),
                range=_json_range_to_model(location_data["range"]),
            )
            sel_range = None
            if s.get("selection_range"):
                sel_range = _json_range_to_model(s["selection_range"])

            symbols.append(
                Symbol(
                    project=self._project_model,
                    name=s["name"],
                    qualified_name=s.get("qualified_name"),
                    kind=s.get("kind", "unknown"),
                    location=loc,
                    selection_range=sel_range,
                    container_name=s.get("container_name"),
                    deprecated=s.get("deprecated", False),
                )
            )
        return symbols

    # ── Workspace Symbols ────────────────────────────────────────────

    def workspace_symbols(self, query: str) -> list[Symbol]:
        """Search for symbols matching *query* across the project."""
        try:
            raw_json: str = self._inner.workspace_symbols(query)
        except Exception as e:
            if "closed" in str(e).lower():
                raise ProjectClosedError(str(e)) from e
            raise

        data = json.loads(raw_json)
        symbols: list[Symbol] = []
        for s in data:
            location_data = s["location"]
            loc = ModelFileRange(
                path=_string_path_to_typath(location_data["path"]),
                range=_json_range_to_model(location_data["range"]),
            )
            sel_range = None
            if s.get("selection_range"):
                sel_range = _json_range_to_model(s["selection_range"])

            symbols.append(
                Symbol(
                    project=self._project_model,
                    name=s["name"],
                    qualified_name=s.get("qualified_name"),
                    kind=s.get("kind", "unknown"),
                    location=loc,
                    selection_range=sel_range,
                    container_name=s.get("container_name"),
                    deprecated=s.get("deprecated", False),
                )
            )
        return symbols

    # ── Goto Definition ──────────────────────────────────────────────

    def goto_definition(
        self, path: str | StdPath, line: int, column: int
    ) -> list[DefinitionTarget]:
        """Navigate to the definition of the symbol at *(line, column)*."""
        return self._goto("goto_definition", path, line, column)

    # ── Goto Declaration ─────────────────────────────────────────────

    def goto_declaration(
        self, path: str | StdPath, line: int, column: int
    ) -> list[DefinitionTarget]:
        """Navigate to the declaration of the symbol at *(line, column)*."""
        return self._goto("goto_declaration", path, line, column)

    # ── Goto Type Definition ─────────────────────────────────────────

    def goto_type_definition(
        self, path: str | StdPath, line: int, column: int
    ) -> list[DefinitionTarget]:
        """Navigate to the type definition of the symbol at *(line, column)*."""
        return self._goto("goto_type_definition", path, line, column)

    def _goto(
        self, method: str, path: str | StdPath, line: int, column: int
    ) -> list[DefinitionTarget]:
        """Shared implementation for all goto-* methods."""
        try:
            raw_json: str = getattr(self._inner, method)(str(path), line, column)
        except Exception as e:
            if "closed" in str(e).lower():
                raise ProjectClosedError(str(e)) from e
            if any(kw in str(e).lower() for kw in ("position", "column", "line")):
                raise PositionError(str(e)) from e
            if any(kw in str(e).lower() for kw in ("resolve", "not in project")):
                raise PathResolutionError(str(e)) from e
            raise

        data = json.loads(raw_json)
        targets: list[DefinitionTarget] = []
        for t in data:
            sel_range = None
            if t.get("selection_range"):
                sel_range = _json_range_to_model(t["selection_range"])

            symbol = None
            if t.get("symbol"):
                sym_data = t["symbol"]
                loc_data = sym_data["location"]
                sym_loc = ModelFileRange(
                    path=_string_path_to_typath(loc_data["path"]),
                    range=_json_range_to_model(loc_data["range"]),
                )
                sym_sel = None
                if sym_data.get("selection_range"):
                    sym_sel = _json_range_to_model(sym_data["selection_range"])
                symbol = Symbol(
                    project=self._project_model,
                    name=sym_data["name"],
                    qualified_name=sym_data.get("qualified_name"),
                    kind=sym_data.get("kind", "unknown"),
                    location=sym_loc,
                    selection_range=sym_sel,
                    container_name=sym_data.get("container_name"),
                    deprecated=sym_data.get("deprecated", False),
                )

            targets.append(
                DefinitionTarget(
                    project=self._project_model,
                    path=_string_path_to_typath(t["path"]),
                    range=_json_range_to_model(t["range"]),
                    selection_range=sel_range,
                    symbol=symbol,
                    module_name=t.get("module_name"),
                )
            )
        return targets

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
            raw_json: str = self._inner.find_references(
                str(path), line, column, include_declaration
            )
        except Exception as e:
            if "closed" in str(e).lower():
                raise ProjectClosedError(str(e)) from e
            if any(kw in str(e).lower() for kw in ("position", "column", "line")):
                raise PositionError(str(e)) from e
            raise

        data = json.loads(raw_json)
        refs: list[Reference] = []
        for r in data:
            refs.append(
                Reference(
                    project=self._project_model,
                    path=_string_path_to_typath(r["path"]),
                    range=_json_range_to_model(r["range"]),
                    kind=r.get("kind", ReferenceKind.OTHER),
                )
            )
        return refs

    # ── Hover ────────────────────────────────────────────────────────

    def hover(
        self, path: str | StdPath, line: int, column: int
    ) -> Optional[HoverResult]:
        """Get hover information for the symbol at *(line, column)*.

        Returns ``None`` when no hover information is available.
        """
        try:
            raw_json: Optional[str] = self._inner.hover(str(path), line, column)
        except Exception as e:
            if "closed" in str(e).lower():
                raise ProjectClosedError(str(e)) from e
            if any(kw in str(e).lower() for kw in ("position", "column", "line")):
                raise PositionError(str(e)) from e
            raise

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

    # ── Lifecycle ────────────────────────────────────────────────────

    def reload(self) -> None:
        """Reload the project, clearing cached diagnostics and re-scanning."""
        try:
            self._inner.reload()
        except Exception as e:
            if "closed" in str(e).lower():
                raise ProjectClosedError(str(e)) from e
            raise

    def close(self) -> None:
        """Close the project and free Rust-side resources."""
        self._inner.close()
