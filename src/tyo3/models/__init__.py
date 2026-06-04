"""TyO3 domain models."""

# Spec-anticipation models (no backend yet) — available for backward
# compatibility via direct import but not part of the public API.
from tyo3.models._spec import BackendInfo, TyProjectConfig  # noqa: F401
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
    ProjectStatus,
)
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverContent,
    HoverContentKind,
    HoverResult,
    NameOccurrence,
    Reference,
    ReferenceKind,
    ReferenceRole,
    TypeHierarchy,
    TypeHierarchyItem,
)
from tyo3.models.symbols import Symbol, SymbolKind

__all__ = [  # noqa: F405
    # Core
    "ProjectStatus",
    "FileCategory",
    "CoordinateMode",
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
    # Occurrences
    "ReferenceRole",
    "NameOccurrence",
]
