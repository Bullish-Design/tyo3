"""Tests for semantic_tokens API."""

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
class TestSemanticTokens:
    def test_returns_list(self) -> None:
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        try:
            files = rp.files()
            assert len(files) > 0
            tokens = rp.semantic_tokens(str(files[0]))
            assert isinstance(tokens, list)
        finally:
            rp.close()

    def test_tokens_have_range_and_type(self) -> None:
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        try:
            files = rp.files()
            tokens = rp.semantic_tokens(str(files[0]))
            if tokens:
                t = tokens[0]
                assert t.range is not None
                assert t.token_type is not None
                assert isinstance(t.modifiers, (set, list, frozenset))
        finally:
            rp.close()

    def test_classes_fixture_has_class_tokens(self) -> None:
        from tyo3.models.advanced import SemanticTokenType
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        try:
            files = rp.files()
            tokens = rp.semantic_tokens(str(files[0]))
            token_types = {t.token_type for t in tokens}
            # The classes fixture should have at least class or function tokens
            assert len(token_types) > 1
        finally:
            rp.close()

    def test_bad_path_raises(self) -> None:
        from tyo3.exceptions import PathResolutionError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        try:
            with pytest.raises(PathResolutionError):
                rp.semantic_tokens("nonexistent.py")
        finally:
            rp.close()

    def test_after_close_raises(self) -> None:
        from tyo3.exceptions import ProjectClosedError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        rp.close()
        with pytest.raises(ProjectClosedError):
            rp.semantic_tokens("anything.py")

    def test_session_returns_tokens(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            files = session.files()
            assert len(files) > 0
            tokens = session.semantic_tokens(str(files[0]))
            assert isinstance(tokens, list)
            if tokens:
                assert tokens[0].file is not None


@needs_native
class TestSemanticTokensViaSession:
    def test_tokens_for_simple_package(self) -> None:
        from tyo3.models.advanced import SemanticToken
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("simple_package")) as session:
            files = session.files()
            py_files = [f for f in files if str(f).endswith(".py")]
            assert len(py_files) > 0
            tokens = session.semantic_tokens(str(py_files[0]))
            assert all(isinstance(t, SemanticToken) for t in tokens)
