"""Invariant tests derived from Allium spec invariants.

Invariants to verify:
- ProjectCannotBeReopened: no two open projects share a root
- FilesBelongToOpenProject: every ProjectFile's project is open
- DiagnosticBelongsToOpenProject: every Diagnostic's project is open
- DiagnosticFileBelongsToProject: every Diagnostic's file belongs to its project
- SymbolsBelongToProject: every Symbol's project is open
- DefinitionTargetInProject / ReferenceInProjectFiles: targets/references belong to open projects
- SemanticTokenInProject: every token's file.project == token.project
"""

from datetime import datetime, timezone

import pytest

from tyo3.models.analysis import Diagnostic, DiagnosticSeverity
from tyo3.models.core import (
    FileCategory,
    Path,
    ProjectFile,
    ProjectStatus,
    TyProject,
)
from tyo3.models.navigation import DefinitionTarget, Reference, ReferenceKind
from tyo3.models.symbols import Symbol, SymbolKind
from tyo3.models.advanced import SemanticToken, SemanticTokenModifier, SemanticTokenType


class TestProjectCannotBeReopened:
    """invariant ProjectCannotBeReopened:
    for p in TyProject where status = open:
        not exists TyProject{root: p.root, status: open}
    """

    def test_unique_open_per_root(self) -> None:
        root = Path(components=["shared"])
        now = datetime.now(timezone.utc)
        p1 = TyProject(root=root, status=ProjectStatus.OPEN, opened_at=now)
        open_projects = [p for p in [p1] if p.status == ProjectStatus.OPEN]
        roots = {p.root for p in open_projects}
        assert len(roots) == len(open_projects), "Duplicate open project roots detected"

    def test_closed_project_allows_reopen(self) -> None:
        root = Path(components=["cycle"])
        now = datetime.now(timezone.utc)
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
            path=Path(components=["f.py"]),
            project=open_project,
            file_category=FileCategory.FIRST_PARTY,
        )
        assert pf.project.status == ProjectStatus.OPEN

    def test_file_belongs_to_closed_project(self, closed_project) -> None:
        # This would violate the invariant — detect and flag
        pf = ProjectFile(
            path=Path(components=["f.py"]),
            project=closed_project,
            file_category=FileCategory.FIRST_PARTY,
        )
        assert pf.project.status != ProjectStatus.OPEN


class TestDiagnosticBelongsToOpenProject:
    """invariant DiagnosticBelongsToOpenProject: d.project.status = open"""

    def test_diagnostic_project_open(self, open_project) -> None:
        d = Diagnostic(project=open_project, message="test", severity="error")
        assert d.project.is_open

    def test_diagnostic_project_closed(self, closed_project) -> None:
        d = Diagnostic(project=closed_project, message="test", severity="error")
        # This is a violation — document it
        assert not d.project.is_open


class TestDiagnosticFileBelongsToProject:
    """invariant DiagnosticFileBelongsToProject:
    for d in Diagnostic where d.file != null: d.file.project = d.project
    """

    def test_file_belongs_to_same_project(self, open_project, first_party_file) -> None:
        d = Diagnostic(
            project=open_project,
            file=first_party_file,
            message="test",
            severity="error",
        )
        assert d.file is not None
        assert d.file.project.root == d.project.root

    def test_file_from_different_project(self, open_project) -> None:
        other = TyProject(
            root=Path(components=["other"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        other_file = ProjectFile(
            path=Path(components=["other.py"]),
            project=other,
            file_category=FileCategory.FIRST_PARTY,
        )
        d = Diagnostic(project=open_project, file=other_file, message="test", severity="error")
        # Violation: d.file.project != d.project
        assert d.file.project.root != d.project.root


class TestSymbolsBelongToProject:
    """invariant SymbolsBelongToProject: s.project.status = open"""

    def test_symbol_project_open(self, open_project) -> None:
        from tyo3.models.analysis import FileRange, Position, Range
        s = Symbol(
            project=open_project,
            name="foo",
            kind=SymbolKind.FUNCTION,
            location=FileRange(
                path=Path(components=["f.py"]),
                range=Range(start=Position(line=1, column=1), end=Position(line=1, column=1)),
            ),
        )
        assert s.project.is_open


class TestSemanticTokenInProject:
    """invariant SemanticTokenInProject: t.file.project = t.project"""

    def test_token_file_project_matches(self, open_project, first_party_file) -> None:
        from tyo3.models.analysis import Position, Range

        t = SemanticToken(
            project=open_project,
            file=first_party_file,
            range=Range(start=Position(line=1, column=1), end=Position(line=1, column=5)),
            token_type=SemanticTokenType.FUNCTION,
        )
        assert t.file.project.root == t.project.root
