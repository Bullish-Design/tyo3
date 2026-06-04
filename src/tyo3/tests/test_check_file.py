"""Tests for TyO3Session.check_file() path filtering."""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)

# Check if native extension is available
try:
    from tyo3 import TyO3Session

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

from tyo3.exceptions import ProjectClosedError

# ── Path helpers ──────────────────────────────────────────────────────────

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    """Return the absolute path to a fixture directory."""
    return str((FIXTURES_DIR / name).resolve())


needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

# ── Shared cache (session-scoped, via conftest) ──────────────────────────


def get_project(fixture_name: str) -> TyO3Session:
    from tyo3.tests.conftest import shared_project

    return shared_project(fixture_name)


def _make_diagnostic(file_path: str) -> Diagnostic:
    return Diagnostic(
        file=file_path,
        range=Range(
            start=Position(line=1, column=1),
            end=Position(line=1, column=10),
        ),
        severity=DiagnosticSeverity.ERROR,
        message="test error",
    )


class TestCheckFileFiltering:
    """Verify that check_file actually filters by the given path."""

    def test_filter_matches_target_file(self):
        """Diagnostics for the target file should be included."""
        d1 = _make_diagnostic("src/main.py")
        d2 = _make_diagnostic("src/other.py")
        result = CheckResult(diagnostics=[d1, d2], files_checked=2)

        # Simulate what check_file does: filter by path
        target = "src/main.py"
        filtered = [d for d in result.diagnostics if d.file is not None and str(d.file) == target]
        assert len(filtered) == 1
        assert filtered[0].file == "src/main.py"

    def test_filter_excludes_other_files(self):
        """Diagnostics for other files should be excluded."""
        d1 = _make_diagnostic("src/other.py")
        result = CheckResult(diagnostics=[d1], files_checked=1)

        target = "src/main.py"
        filtered = [d for d in result.diagnostics if d.file is not None and str(d.file) == target]
        assert len(filtered) == 0

    def test_filter_handles_no_file_diagnostics(self):
        """Diagnostics with file=None should be excluded."""
        d = Diagnostic(message="generic error")
        result = CheckResult(diagnostics=[d])

        target = "src/main.py"
        filtered = [d for d in result.diagnostics if d.file is not None and str(d.file) == target]
        assert len(filtered) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Integration tests — exercise the Rust check_file() backend
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestCheckFileIntegration:
    """Test check_file() through the real Rust backend."""

    def test_check_file_returns_result(self) -> None:
        """check_file() should return a CheckResult for the target file."""
        rp = get_project("diagnostic_targets")
        result = rp.check_file("errors.py")
        assert result is not None
        assert isinstance(result.diagnostics, list)
        assert result.files_checked == 1

    def test_check_file_returns_only_target_diagnostics(self) -> None:
        """All diagnostics returned by check_file() should be for the target file."""
        rp = get_project("diagnostic_targets")
        result = rp.check_file("errors.py")
        for d in result.diagnostics:
            assert d.file is not None, f"Diagnostic has no file: {d.message}"
            assert "errors.py" in d.file, f"Diagnostic file '{d.file}' does not match 'errors.py'"

    def test_check_file_on_multi_file_project(self) -> None:
        """check_file() on a multi-file project filters correctly."""
        rp = get_project("imports")
        # math_ops.py is a clean file — should have 0 diagnostics
        result = rp.check_file("math_ops.py")
        for d in result.diagnostics:
            assert "math_ops.py" in d.file, f"Got diagnostic for wrong file: {d.file} -> {d.message}"

    def test_check_file_after_close_raises(self) -> None:
        """check_file() should raise ProjectClosedError after close()."""
        # Needs own instance since it closes the project
        rp = TyO3Session(fixture_path("simple_package"))
        rp.close()
        with pytest.raises(ProjectClosedError):
            rp.check_file("main.py")

    def test_check_file_nonexistent_raises(self) -> None:
        """check_file() should raise on a nonexistent file path."""
        from tyo3.exceptions import PathResolutionError

        rp = get_project("simple_package")
        with pytest.raises(PathResolutionError):
            rp.check_file("nonexistent.py")

    def test_check_file_vs_check_consistency(self) -> None:
        """check() should include all diagnostics that check_file() returns for any file."""
        rp = get_project("diagnostic_targets")
        full_result = rp.check()
        file_result = rp.check_file("errors.py")

        # Every diagnostic from check_file() should be present in check()
        full_messages = {d.message for d in full_result.diagnostics}
        for d in file_result.diagnostics:
            assert d.message in full_messages, f"check_file diagnostic not found in check: {d.message}"
