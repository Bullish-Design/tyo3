"""Analysis domain models: type checking, diagnostic reporting.

Derived from tyo3-analysis.allium
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── Value Types ──────────────────────────────────────────────────────────


class Position(BaseModel):
    """A 1-based position in a file."""

    model_config = ConfigDict(from_attributes=True)

    line: int  # 1-based
    column: int  # 1-based, Unicode codepoints

    @model_validator(mode="after")
    def _positive(self) -> Position:
        if self.line < 1 or self.column < 1:
            raise ValueError(f"Position must be 1-based: got line={self.line}, column={self.column}")
        return self


class Range(BaseModel):
    """A range between two positions."""

    model_config = ConfigDict(from_attributes=True)

    start: Position
    end: Position

    @model_validator(mode="after")
    def _start_before_end(self) -> Range:
        s, e = self.start, self.end
        if (s.line, s.column) > (e.line, e.column):
            raise ValueError(f"Range start ({s.line}:{s.column}) must not be after end ({e.line}:{e.column})")
        return self


class FileRange(BaseModel):
    """A range within a specific file."""

    model_config = ConfigDict(from_attributes=True)

    path: PurePosixPath
    range: Range


class CheckResult(BaseModel):
    """Result of a type-check invocation."""

    model_config = ConfigDict(from_attributes=True)

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


class SyncResult(BaseModel):
    """Delta produced by a write to the head."""

    model_config = ConfigDict(from_attributes=True)

    revision: int
    created: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    moved: list[str] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    orphaned: list[str] = Field(default_factory=list)
    identity_extracted: int = 0
    identity_scope_files: int = 0
    authored: list[str] = Field(default_factory=list)
    authored_needs_review: list[str] = Field(default_factory=list)
    authored_orphaned: list[str] = Field(default_factory=list)
    project_changed: bool = False
    custom_stdlib_changed: bool = False
    rescan: bool = False


class Diagnostic(BaseModel):
    """A type-checking diagnostic for a specific location in a project file."""

    model_config = ConfigDict(from_attributes=True)

    file: str | None = None
    range: Range | None = None
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    code: str | None = None
    message: str
    details: list[str] = Field(default_factory=list)


__all__ = [
    "Position",
    "Range",
    "FileRange",
    "CheckResult",
    "SyncResult",
    "DiagnosticSeverity",
    "Diagnostic",
]
