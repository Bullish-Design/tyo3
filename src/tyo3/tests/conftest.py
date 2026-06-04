"""Shared test fixtures for TyO3 test suite."""

from __future__ import annotations

from pathlib import Path as StdPath
from pathlib import PurePosixPath

import pytest

from tyo3.models.advanced import SemanticToken, SemanticTokenModifier, SemanticTokenType
from tyo3.models.analysis import Diagnostic, DiagnosticSeverity, FileRange, Position, Range
from tyo3.models.navigation import DefinitionTarget, Reference, ReferenceKind
from tyo3.models.symbols import Symbol, SymbolKind

# ── Native extension detection ──────────────────────────────────────────

try:
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")


# ── Shared caches (session-scoped, shared across all test modules) ─────

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def _fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


# Session-scoped caches for expensive objects
_shared_session_cache: dict[str, object] = {}
_shared_graph_cache: dict[str, object] = {}


@pytest.fixture(autouse=True, scope="session")
def _shared_cache_cleanup():
    """Clean up all shared caches at end of test session."""
    yield
    for session in _shared_session_cache.values():
        try:
            session.close()  # type: ignore[union-attr]
        except Exception:
            pass
    _shared_session_cache.clear()
    _shared_graph_cache.clear()


def shared_project(fixture_name: str):
    """Get or create a cached TyO3Session (session-scoped, read-only use only).

    Alias for shared_session — both return the same TyO3Session.
    """
    return shared_session(fixture_name)


def shared_graph(fixture_name: str):
    """Get or create a cached CodeGraph (session-scoped, read-only use only)."""
    if fixture_name not in _shared_graph_cache:
        from tyo3.graph import CodeGraph
        from tyo3.session import TyO3Session

        session = TyO3Session(_fixture_path(fixture_name))
        _shared_session_cache[fixture_name] = session
        _shared_graph_cache[fixture_name] = CodeGraph.build(session)
    return _shared_graph_cache[fixture_name]


def shared_session(fixture_name: str):
    """Get or create a cached TyO3Session (session-scoped, read-only use only)."""
    if fixture_name not in _shared_session_cache:
        shared_graph(fixture_name)  # builds both session and graph
    return _shared_session_cache[fixture_name]


def get_graph(fixture_name: str):
    """Alias for shared_graph — get a cached CodeGraph."""
    return shared_graph(fixture_name)


def get_session(fixture_name: str):
    """Alias for shared_session — get a cached TyO3Session."""
    return shared_session(fixture_name)


# ── Fixture helpers ─────────────────────────────────────────────────────


def make_path(components: list[str]) -> PurePosixPath:
    return PurePosixPath("/".join(components))


# ── Pytest fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def path() -> PurePosixPath:
    return make_path(["home", "user", "project"])


@pytest.fixture
def position() -> Position:
    return Position(line=1, column=1)


@pytest.fixture
def range_() -> Range:
    return Range(start=Position(line=1, column=1), end=Position(line=10, column=5))


@pytest.fixture
def diagnostic() -> Diagnostic:
    return Diagnostic(
        file="home/user/project/main.py",
        range=Range(start=Position(line=5, column=1), end=Position(line=5, column=20)),
        severity=DiagnosticSeverity.ERROR,
        code="type-arg",
        message="Missing type argument in generic",
        details=["TypeVar", "bound"],
    )


@pytest.fixture
def symbol() -> Symbol:
    return Symbol(
        name="MyClass",
        qualified_name="my_module.MyClass",
        kind=SymbolKind.CLASS,
        location=FileRange(
            path=PurePosixPath("home/user/project/main.py"),
            range=Range(start=Position(line=1, column=1), end=Position(line=10, column=1)),
        ),
        deprecated=False,
    )


@pytest.fixture
def definition_target() -> DefinitionTarget:
    return DefinitionTarget(
        path=make_path(["home", "user", "project", "main.py"]),
        range=Range(start=Position(line=1, column=1), end=Position(line=10, column=5)),
    )


@pytest.fixture
def reference() -> Reference:
    return Reference(
        path=make_path(["home", "user", "project", "main.py"]),
        range=Range(start=Position(line=15, column=10), end=Position(line=15, column=20)),
        kind=ReferenceKind.READ,
    )


@pytest.fixture
def semantic_token() -> SemanticToken:
    return SemanticToken(
        file=make_path(["home", "user", "project", "main.py"]),
        range=Range(start=Position(line=1, column=1), end=Position(line=1, column=10)),
        token_type=SemanticTokenType.FUNCTION,
        modifiers={SemanticTokenModifier.DEFINITION},
    )
