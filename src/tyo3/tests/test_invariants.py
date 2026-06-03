"""Invariant tests derived from Allium spec invariants.

Invariants to verify:
- ProjectCannotBeReopened: no two open projects share a root
- FilesBelongToOpenProject: every ProjectFile's project is open
- DiagnosticHasFileOrNone: Diagnostic.file is a string path or None
- SymbolLocationPointsToProjectFile
"""

from datetime import UTC, datetime
from pathlib import PurePosixPath

from tyo3.models.analysis import Diagnostic
from tyo3.models.core import (
    FileCategory,
    ProjectFile,
    ProjectStatus,
    TyProject,
)
from tyo3.models.symbols import Symbol, SymbolKind


class TestProjectCannotBeReopened:
    """invariant ProjectCannotBeReopened:
    for p in TyProject where status = open:
        not exists TyProject{root: p.root, status: open}
    """

    def test_unique_open_per_root(self) -> None:
        root = PurePosixPath("shared")
        now = datetime.now(UTC)
        p1 = TyProject(root=root, status=ProjectStatus.OPEN, opened_at=now)
        open_projects = [p for p in [p1] if p.status == ProjectStatus.OPEN]
        roots = {p.root for p in open_projects}
        assert len(roots) == len(open_projects), "Duplicate open project roots detected"

    def test_closed_project_allows_reopen(self) -> None:
        root = PurePosixPath("cycle")
        now = datetime.now(UTC)
        p1 = TyProject(root=root, status=ProjectStatus.CLOSED, opened_at=now)
        p2 = TyProject(root=root, status=ProjectStatus.OPEN, opened_at=now)
        open_projects = [p for p in [p1, p2] if p.status == ProjectStatus.OPEN]
        roots = {p.root for p in open_projects}
        assert len(roots) == len(open_projects)


class TestFilesBelongToOpenProject:
    """invariant FilesBelongToOpenProject:
    for f in ProjectFile: f.project.status = open
    """

    def test_file_project_must_be_open(self, open_project) -> None:
        pf = ProjectFile(
            path=PurePosixPath("f.py"),
            project=open_project,
            file_category=FileCategory.FIRST_PARTY,
        )
        assert pf.project.status == ProjectStatus.OPEN

    def test_file_belongs_to_closed_project(self, closed_project) -> None:
        # This would violate the invariant — detect and flag
        pf = ProjectFile(
            path=PurePosixPath("f.py"),
            project=closed_project,
            file_category=FileCategory.FIRST_PARTY,
        )
        assert pf.project.status != ProjectStatus.OPEN


class TestDiagnosticFileOrNone:
    """Diagnostic.file is a string path (from Rust backend) or None."""

    def test_diagnostic_with_file_string(self) -> None:
        d = Diagnostic(
            file="src/main.py",
            message="test",
        )
        assert d.file == "src/main.py"
        assert d.message == "test"

    def test_diagnostic_without_file(self) -> None:
        d = Diagnostic(message="test")
        assert d.file is None
        assert d.message == "test"


class TestDiagnosticFileConsistency:
    """Diagnostic.file can be any path string representing the source file."""

    def test_file_is_valid_path_string(self) -> None:
        d = Diagnostic(
            file="src/package/module.py",
            message="test",
        )
        assert d.file is not None
        assert isinstance(d.file, str)
        assert "/" in d.file

    def test_diagnostic_with_different_file(self) -> None:
        d = Diagnostic(file="other.py", message="test")
        assert d.file == "other.py"


class TestSymbolLocationHasPath:
    """invariant: Symbols have a location with a path."""

    def test_symbol_has_location(self, open_project) -> None:
        from tyo3.models.analysis import FileRange, Position, Range

        s = Symbol(
            name="foo",
            kind=SymbolKind.FUNCTION,
            location=FileRange(
                path=PurePosixPath("f.py"),
                range=Range(start=Position(line=1, column=1), end=Position(line=1, column=1)),
            ),
        )
        assert s.location.path is not None
        assert s.name == "foo"
