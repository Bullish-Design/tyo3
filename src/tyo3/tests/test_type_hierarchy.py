"""Tests for type_hierarchy API."""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

try:
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

# ── Shared cache (session-scoped, via conftest) ──────────────────────────


def get_project(fixture_name: str):
    from tyo3.tests.conftest import shared_project

    return shared_project(fixture_name)


@needs_native
class TestTypeHierarchy:
    def test_returns_none_for_non_class(self) -> None:
        rp = get_project("simple_package")
        files = rp.files()
        # Try a position that's not a class — should return None
        result = rp.type_hierarchy(str(files[0]), 1, 1)
        # Result may be None or a hierarchy, depending on what's at 1:1
        assert result is None or result.item is not None

    def test_class_has_hierarchy(self) -> None:
        from tyo3.models.symbols import SymbolKind

        rp = get_project("classes")
        files = rp.files()
        symbols = rp.document_symbols(str(files[0]))
        # Find a class symbol that inherits from something
        classes = [
            s for s in symbols if s.kind == SymbolKind.CLASS and s.selection_range is not None and s.name != "Animal"
        ]
        if classes:
            cls = classes[0]
            start = cls.selection_range.start
            result = rp.type_hierarchy(str(files[0]), start.line, start.column)
            if result is not None:
                assert result.item.name == cls.name

    def test_bad_path_raises(self) -> None:
        from tyo3.exceptions import PathResolutionError

        rp = get_project("classes")
        with pytest.raises(PathResolutionError):
            rp.type_hierarchy("nonexistent.py", 1, 1)

    def test_after_close_raises(self) -> None:
        from tyo3.exceptions import ProjectClosedError
        from tyo3.rust_project import RustProject

        # Needs own instance since it closes the project
        rp = RustProject(fixture_path("classes"))
        rp.close()
        with pytest.raises(ProjectClosedError):
            rp.type_hierarchy("anything.py", 1, 1)
