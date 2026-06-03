"""TyO3 domain models."""

from tyo3.models.advanced import (
    SemanticToken,
    SemanticTokenModifier,
    SemanticTokenType,
)
from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    FileRange,
    Position,
    Range,
)
from tyo3.models.core import (
    CoordinateMode,
    FileCategory,
    ProjectFile,
    ProjectStatus,
    TyProject,
)
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverContent,
    HoverContentKind,
    HoverResult,
    Reference,
    ReferenceKind,
    TypeHierarchy,
    TypeHierarchyItem,
)
from tyo3.models.symbols import Symbol, SymbolKind

# Spec-anticipation models (no backend yet)
from tyo3.models._spec import BackendInfo, TyProjectConfig

__all__ = [  # noqa: F405
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
    "TypeHierarchyItem",
    "TypeHierarchy",
    # Advanced
    "SemanticTokenType",
    "SemanticTokenModifier",
    "SemanticToken",
]
