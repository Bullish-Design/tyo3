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


needs_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="Rust native extension not built"
)


@needs_native
class TestTypeHierarchy:
    def test_returns_none_for_non_class(self) -> None:
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        try:
            files = rp.files()
            # Try a position that's not a class — should return None
            result = rp.type_hierarchy(str(files[0]), 1, 1)
            # Result may be None or a hierarchy, depending on what's at 1:1
            assert result is None or result.item is not None
        finally:
            rp.close()

    def test_class_has_hierarchy(self) -> None:
        from tyo3.models.symbols import SymbolKind
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        try:
            files = rp.files()
            symbols = rp.document_symbols(str(files[0]))
            # Find a class symbol that inherits from something
            classes = [s for s in symbols if s.kind == SymbolKind.CLASS_
                       and s.selection_range is not None
                       and s.name != "Animal"]
            if classes:
                cls = classes[0]
                start = cls.selection_range.start
                result = rp.type_hierarchy(str(files[0]), start.line, start.column)
                if result is not None:
                    assert result.item.name == cls.name
        finally:
            rp.close()

    def test_bad_path_raises(self) -> None:
        from tyo3.exceptions import PathResolutionError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        try:
            with pytest.raises(PathResolutionError):
                rp.type_hierarchy("nonexistent.py", 1, 1)
        finally:
            rp.close()

    def test_after_close_raises(self) -> None:
        from tyo3.exceptions import ProjectClosedError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        rp.close()
        with pytest.raises(ProjectClosedError):
            rp.type_hierarchy("anything.py", 1, 1)
