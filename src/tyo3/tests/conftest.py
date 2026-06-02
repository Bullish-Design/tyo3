"""Shared test fixtures for TyO3 test suite."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from tyo3.models.advanced import SemanticToken, SemanticTokenModifier, SemanticTokenType
from tyo3.models.analysis import Diagnostic, DiagnosticSeverity, Position, Range
from tyo3.models.core import (
    FileCategory,
    ProjectFile,
    ProjectStatus,
    TyProject,
    TyProjectConfig,
)
from tyo3.models.navigation import DefinitionTarget, Reference, ReferenceKind
from tyo3.models.symbols import Symbol, SymbolKind
from tyo3.services.advanced_service import AdvancedService
from tyo3.services.analysis_service import AnalysisService
from tyo3.services.navigation_service import NavigationService
from tyo3.services.project_service import ProjectService
from tyo3.services.symbol_service import SymbolService

# ── Fixture helpers ─────────────────────────────────────────────────────


def make_path(components: list[str]) -> PurePosixPath:
    return PurePosixPath("/".join(components))


def make_project(
    root_components: list[str] | None = None,
    status: str = ProjectStatus.OPEN,
) -> TyProject:
    return TyProject(
        root=make_path(root_components or ["home", "user", "project"]),
        status=status,
        opened_at=datetime.now(UTC),
    )


def make_file(
    project: TyProject,
    path_components: list[str] | None = None,
    category: str = FileCategory.FIRST_PARTY,
) -> ProjectFile:
    return ProjectFile(
        path=make_path(path_components or ["home", "user", "project", "main.py"]),
        project=project,
        file_category=category,
    )


# ── Pytest fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def path() -> PurePosixPath:
    return make_path(["home", "user", "project"])


@pytest.fixture
def open_project() -> TyProject:
    return make_project(status=ProjectStatus.OPEN)


@pytest.fixture
def closed_project() -> TyProject:
    return make_project(status=ProjectStatus.CLOSED)


@pytest.fixture
def error_project() -> TyProject:
    return make_project(status=ProjectStatus.ERROR)


@pytest.fixture
def first_party_file(open_project: TyProject) -> ProjectFile:
    return make_file(open_project, category=FileCategory.FIRST_PARTY)


@pytest.fixture
def vendored_file(open_project: TyProject) -> ProjectFile:
    return make_file(
        open_project,
        path_components=["home", "user", "project", "vendor", "lib.py"],
        category=FileCategory.VENDORED,
    )


@pytest.fixture
def stub_file(open_project: TyProject) -> ProjectFile:
    return make_file(
        open_project,
        path_components=["home", "user", "project", "stubs", "mod.pyi"],
        category=FileCategory.STUB,
    )


@pytest.fixture
def dependency_file(open_project: TyProject) -> ProjectFile:
    return make_file(
        open_project,
        path_components=["home", "user", ".venv", "lib.py"],
        category=FileCategory.DEPENDENCY,
    )


@pytest.fixture
def project_config() -> TyProjectConfig:
    return TyProjectConfig()


@pytest.fixture
def backends() -> list: ...


@pytest.fixture
def position() -> Position:
    return Position(line=1, column=1)


@pytest.fixture
def range_() -> Range:
    return Range(start=Position(line=1, column=1), end=Position(line=10, column=5))


@pytest.fixture
def diagnostic(open_project: TyProject, first_party_file: ProjectFile) -> Diagnostic:
    return Diagnostic(
        file=first_party_file,
        range=Range(start=Position(line=5, column=1), end=Position(line=5, column=20)),
        severity=DiagnosticSeverity.ERROR,
        code="type-arg",
        message="Missing type argument in generic",
        details=["TypeVar", "bound"],
    )


@pytest.fixture
def symbol(open_project: TyProject, first_party_file: ProjectFile) -> Symbol:
    from tyo3.models.analysis import FileRange, Position, Range

    return Symbol(
        name="MyClass",
        qualified_name="my_module.MyClass",
        kind=SymbolKind.CLASS_,
        location=FileRange(
            path=first_party_file.path,
            range=Range(start=Position(line=1, column=1), end=Position(line=10, column=1)),
        ),
        deprecated=False,
    )


@pytest.fixture
def definition_target(open_project: TyProject) -> DefinitionTarget:
    return DefinitionTarget(
        path=make_path(["home", "user", "project", "main.py"]),
        range=Range(start=Position(line=1, column=1), end=Position(line=10, column=5)),
    )


@pytest.fixture
def reference(open_project: TyProject) -> Reference:
    return Reference(
        path=make_path(["home", "user", "project", "main.py"]),
        range=Range(start=Position(line=15, column=10), end=Position(line=15, column=20)),
        kind=ReferenceKind.READ,
    )


@pytest.fixture
def semantic_token(open_project: TyProject, first_party_file: ProjectFile) -> SemanticToken:
    return SemanticToken(
        file=first_party_file,
        range=Range(start=Position(line=1, column=1), end=Position(line=1, column=10)),
        token_type=SemanticTokenType.FUNCTION,
        modifiers={SemanticTokenModifier.DEFINITION},
    )


# ── Service fixtures ────────────────────────────────────────────────────


@pytest.fixture
def project_service() -> ProjectService:
    return ProjectService()


@pytest.fixture
def analysis_service() -> AnalysisService:
    return AnalysisService()


@pytest.fixture
def symbol_service() -> SymbolService:
    return SymbolService()


@pytest.fixture
def navigation_service() -> NavigationService:
    return NavigationService()


@pytest.fixture
def advanced_service() -> AdvancedService:
    return AdvancedService()
