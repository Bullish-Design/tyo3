"""Domain models derived from Allium specifications."""

from tyo3.models.advanced import SemanticToken, SemanticTokenModifier, SemanticTokenType
from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    FileRange,
    Position,
    Range,
)
from tyo3.models.core import (
    BackendInfo,
    CoordinateMode,
    FileCategory,
    ProjectFile,
    ProjectStatus,
    TyProject,
    TyProjectConfig,
)
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverContent,
    HoverContentKind,
    HoverResult,
    Reference,
    ReferenceKind,
)
from tyo3.models.symbols import Symbol, SymbolKind

__all__ = [
    # Core
    "TyProjectConfig",
    "BackendInfo",
    "ProjectStatus",
    "FileCategory",
    "CoordinateMode",
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
    "HoverContentKind",
    "DefinitionTarget",
    "Reference",
    "HoverContent",
    "HoverResult",
    # Advanced
    "SemanticTokenType",
    "SemanticTokenModifier",
    "SemanticToken",
]
