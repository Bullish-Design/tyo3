"""Domain models derived from Allium specifications."""

from tyo3.models.core import (
    BackendInfo,
    FileCategory,
    Path,
    ProjectFile,
    ProjectStatus,
    TyProject,
    TyProjectConfig,
)
from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    FileRange,
    Position,
    Range,
)
from tyo3.models.symbols import Symbol, SymbolKind
from tyo3.models.navigation import DefinitionTarget, Reference, ReferenceKind
from tyo3.models.advanced import SemanticToken, SemanticTokenModifier, SemanticTokenType

__all__ = [
    # Core
    "Path",
    "TyProjectConfig",
    "BackendInfo",
    "ProjectStatus",
    "FileCategory",
    "TyProject",
    "ProjectFile",
    # Analysis
    "Position",
    "Range",
    "FileRange",
    "CheckResult",
    "DiagnosticSeverity",
    "Diagnostic",
    # Symbols
    "SymbolKind",
    "Symbol",
    # Navigation
    "ReferenceKind",
    "DefinitionTarget",
    "Reference",
    # Advanced
    "SemanticTokenType",
    "SemanticTokenModifier",
    "SemanticToken",
]
