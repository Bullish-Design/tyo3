"""Analysis domain models: type checking, diagnostic reporting.

Derived from tyo3-analysis.allium
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from tyo3.models.core import Path, ProjectFile

# ── Value Types ──────────────────────────────────────────────────────────


class Position(BaseModel):
    """A 1-based position in a file."""
    line: int  # 1-based
    column: int  # 1-based, Unicode codepoints


class Range(BaseModel):
    """A range between two positions."""
    start: Position
    end: Position


class FileRange(BaseModel):
    """A range within a specific file."""
    path: Path
    range: Range


class CheckResult(BaseModel):
    """Result of a type-check invocation."""
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    files_checked: int | None = None
    elapsed_ms: int | None = None


# ── Enums ────────────────────────────────────────────────────────────────


class DiagnosticSeverity(StrEnum):
    """Severity level for a diagnostic."""
    FATAL = "fatal"
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"
    HINT = "hint"


# ── Entities ─────────────────────────────────────────────────────────────


class Diagnostic(BaseModel):
    """A type-checking diagnostic for a specific location in a project file."""
    file: ProjectFile | None = None
    range: Range | None = None
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    code: str | None = None
    message: str
    details: list[str] = Field(default_factory=list)
