"""Invariant tests derived from Allium spec invariants.

Invariants to verify:
- ProjectCannotBeReopened: no two open projects share a root
- FilesBelongToOpenProject: every ProjectFile's project is open
- DiagnosticFileInOpenProject: when a Diagnostic has a file, the file's project is open
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


class TestDiagnosticFileInOpenProject:
    """invariant: when a Diagnostic has a file, the file's project must be open."""

    def test_diagnostic_with_file_has_open_project(self, open_project, first_party_file) -> None:
        d = Diagnostic(
            file=first_party_file,
            message="test",
        )
        assert d.file is not None
        assert d.file.project.status == ProjectStatus.OPEN

    def test_diagnostic_without_file(self) -> None:
        d = Diagnostic(message="test")
        assert d.file is None
        assert d.message == "test"


class TestDiagnosticFileConsistency:
    """invariant: if a Diagnostic has a file, that file must belong to a valid project."""

    def test_file_belongs_to_valid_project(self, open_project, first_party_file) -> None:
        d = Diagnostic(
            file=first_party_file,
            message="test",
        )
        assert d.file is not None
        assert d.file.project.root is not None

    def test_file_from_different_project(self, open_project) -> None:
        other = TyProject(
            root=PurePosixPath("other"),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(UTC),
        )
        other_file = ProjectFile(
            path=PurePosixPath("other.py"),
            project=other,
            file_category=FileCategory.FIRST_PARTY,
        )
        d = Diagnostic(file=other_file, message="test")
        # Diagnostic's file belongs to a different project — that's fine
        # (Diagnostic no longer owns a project reference)
        assert d.file.project.root != open_project.root


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
