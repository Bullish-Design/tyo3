"""Analysis domain models: type checking, diagnostic reporting.

Derived from tyo3-analysis.allium
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field

from tyo3.models.core import Path, ProjectFile, TyProject


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
    files_checked: Optional[int] = None
    elapsed_ms: Optional[int] = None


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
    project: TyProject
    file: Optional[ProjectFile] = None
    range: Optional[Range] = None
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    code: Optional[str] = None
    message: str
    details: set[str] = Field(default_factory=set)
