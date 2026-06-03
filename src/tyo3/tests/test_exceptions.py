"""Unit tests for TyO3 exception hierarchy and error wrapping.

Tests for Phase 5 of RUST_BACKEND_IMPLEMENTATION.md §8.2.

These tests do NOT require the Rust native extension — they test the Python
exception classes directly.
"""

from __future__ import annotations

import warnings
from unittest.mock import MagicMock

import pytest

from tyo3.exceptions import (
    AnalysisError,
    InternalTyError,
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
    TyO3Error,
)


class TestExceptionHierarchy:
    """All TyO3 exceptions inherit from TyO3Error."""

    def test_all_are_tyo3_errors(self) -> None:
        assert issubclass(ProjectOpenError, TyO3Error)
        assert issubclass(ProjectClosedError, TyO3Error)
        assert issubclass(PathResolutionError, TyO3Error)
        assert issubclass(PositionError, TyO3Error)
        assert issubclass(AnalysisError, TyO3Error)
        assert issubclass(InternalTyError, TyO3Error)

    def test_tyo3_error_is_base(self) -> None:
        assert issubclass(TyO3Error, Exception)


class TestProjectOpenError:
    """ProjectOpenError is raised when a project cannot be opened."""

    def test_with_message(self) -> None:
        err = ProjectOpenError("Cannot open project at '/path'")
        assert "Cannot open project at '/path'" in str(err)

    def test_catch_as_tyo3_error(self) -> None:
        with pytest.raises(TyO3Error):
            raise ProjectOpenError("test")


class TestProjectClosedError:
    """ProjectClosedError is raised when operating on a closed project."""

    def test_with_message(self) -> None:
        err = ProjectClosedError("Project is closed")
        assert "closed" in str(err).lower()

    def test_catch_as_tyo3_error(self) -> None:
        with pytest.raises(TyO3Error):
            raise ProjectClosedError("test")


class TestPathResolutionError:
    """PathResolutionError is raised when a file path cannot be resolved."""

    def test_with_message(self) -> None:
        err = PathResolutionError("File not found: main.py")
        assert "File not found" in str(err)

    def test_catch_as_tyo3_error(self) -> None:
        with pytest.raises(TyO3Error):
            raise PathResolutionError("test")


class TestPositionError:
    """PositionError is raised for invalid line/column values."""

    def test_with_message(self) -> None:
        err = PositionError("Line 0 exceeds file length")
        assert "Line 0" in str(err)

    def test_catch_as_tyo3_error(self) -> None:
        with pytest.raises(TyO3Error):
            raise PositionError("test")

    def test_line_zero(self) -> None:
        """Line 0 is invalid (1-based)."""
        with pytest.raises(PositionError):
            raise PositionError("Line must be >= 1")

    def test_column_zero(self) -> None:
        """Column 0 is invalid (1-based)."""
        with pytest.raises(PositionError):
            raise PositionError("Column must be >= 1")


class TestAnalysisError:
    """AnalysisError is raised when the type checker fails."""

    def test_with_message(self) -> None:
        err = AnalysisError("Check failed: internal error")
        assert "Check failed" in str(err)

    def test_catch_as_tyo3_error(self) -> None:
        with pytest.raises(TyO3Error):
            raise AnalysisError("test")


class TestInternalTyError:
    """InternalTyError wraps unexpected internal errors from the Rust engine."""

    def test_with_message(self) -> None:
        err = InternalTyError("Unexpected error from ty_ide")
        assert "Unexpected error" in str(err)

    def test_catch_as_tyo3_error(self) -> None:
        with pytest.raises(TyO3Error):
            raise InternalTyError("test")

    def test_inherits_from_base(self) -> None:
        assert issubclass(InternalTyError, TyO3Error)

    def test_wraps_unexpected_error(self) -> None:
        original = RuntimeError("rust panic")
        wrapped = InternalTyError("Unexpected error: rust panic")
        wrapped.__cause__ = original
        assert "rust panic" in str(wrapped)
        assert wrapped.__cause__ is original


class TestExceptionChaining:
    """Exceptions should preserve cause chains."""

    def test_raise_from_project_closed(self) -> None:
        try:
            try:
                raise RuntimeError("underlying engine failure")
            except RuntimeError as e:
                raise ProjectClosedError("Cannot operate on closed project") from e
        except ProjectClosedError as e:
            assert e.__cause__ is not None
            assert isinstance(e.__cause__, RuntimeError)
            assert "underlying engine failure" in str(e.__cause__)

    def test_raise_from_position_error(self) -> None:
        try:
            try:
                raise ValueError("negative position")
            except ValueError as e:
                raise PositionError("Position must be >= 1") from e
        except PositionError as e:
            assert e.__cause__ is not None
            assert isinstance(e.__cause__, ValueError)


class TestClosedOperations:
    """Calling any public method on a closed RustProject raises ProjectClosedError
    immediately — without crossing the Rust boundary."""

    def _make_closed_rp(self):
        """Create a mock RustProject that is already closed."""
        from tyo3.rust_project import RustProject
        rp = object.__new__(RustProject)
        rp._inner = MagicMock()
        rp._root = None
        rp._closed = True
        return rp

    # ── check() ──────────────────────────────────────────────────

    def test_check_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.check()
        rp._inner.check.assert_not_called()

    # ── check_file() ─────────────────────────────────────────────

    def test_check_file_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.check_file("main.py")
        rp._inner.check_file.assert_not_called()

    # ── files() ──────────────────────────────────────────────────

    def test_files_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.files()
        rp._inner.files.assert_not_called()

    # ── document_symbols() ───────────────────────────────────────

    def test_document_symbols_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.document_symbols("main.py")
        rp._inner.document_symbols.assert_not_called()

    # ── workspace_symbols() ──────────────────────────────────────

    def test_workspace_symbols_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.workspace_symbols("foo")
        rp._inner.workspace_symbols.assert_not_called()

    # ── goto_definition() ────────────────────────────────────────

    def test_goto_definition_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.goto_definition("main.py", 1, 1)
        rp._inner.goto_definition.assert_not_called()

    # ── goto_declaration() ───────────────────────────────────────

    def test_goto_declaration_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.goto_declaration("main.py", 1, 1)
        rp._inner.goto_declaration.assert_not_called()

    # ── goto_type_definition() ───────────────────────────────────

    def test_goto_type_definition_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.goto_type_definition("main.py", 1, 1)
        rp._inner.goto_type_definition.assert_not_called()

    # ── find_references() ────────────────────────────────────────

    def test_find_references_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.find_references("main.py", 1, 1)
        rp._inner.find_references.assert_not_called()

    # ── hover() ──────────────────────────────────────────────────

    def test_hover_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.hover("main.py", 1, 1)
        rp._inner.hover.assert_not_called()

    # ── reload() ─────────────────────────────────────────────────

    def test_reload_raises_when_closed(self) -> None:
        rp = self._make_closed_rp()
        with pytest.raises(ProjectClosedError, match="Project is closed"):
            rp.reload()
        rp._inner.reload.assert_not_called()

    # ── close() is idempotent (already covered in TestCloseIdempotent) ─
    # ── root property is always accessible ────────────────────────

    def test_root_still_accessible_when_closed(self) -> None:
        rp = self._make_closed_rp()
        assert rp.root is None  # root is not guarded


class TestCloseIdempotent:
    """Verify that RustProject.close() is idempotent and __del__ warns."""

    def test_double_close_no_error(self) -> None:
        """Calling close() twice should not raise — second call is a no-op."""
        from tyo3.rust_project import RustProject

        rp = object.__new__(RustProject)
        rp._inner = MagicMock()
        rp._closed = False
        rp._root = None

        rp.close()
        assert rp._closed is True
        rp._inner.close.assert_called_once()

        # Second close should be a no-op
        rp.close()
        rp._inner.close.assert_called_once()  # still only once

    def test_del_warns_when_not_closed(self) -> None:
        """__del__ should emit a ResourceWarning if close() was never called."""
        from tyo3.rust_project import RustProject

        rp = object.__new__(RustProject)
        rp._inner = MagicMock()
        rp._closed = False
        rp._root = None

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            rp.__del__()

        assert len(w) == 1
        assert issubclass(w[0].category, ResourceWarning)
        assert "RustProject was not closed explicitly" in str(w[0].message)
        # After __del__, close() should have been called
        assert rp._closed is True

    def test_del_does_not_warn_when_closed(self) -> None:
        """__del__ should be silent if close() was already called."""
        from tyo3.rust_project import RustProject

        rp = object.__new__(RustProject)
        rp._inner = MagicMock()
        rp._closed = True
        rp._root = None

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            rp.__del__()

        assert len(w) == 0
