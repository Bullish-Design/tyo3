"""Unit tests for TyO3 exception hierarchy and error wrapping.

Tests for Phase 5 of RUST_BACKEND_IMPLEMENTATION.md §8.2.

These tests do NOT require the Rust native extension — they test the Python
exception classes directly.
"""

from __future__ import annotations

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
