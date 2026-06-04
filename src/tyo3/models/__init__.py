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
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverContent,
    HoverContentKind,
    HoverResult,
    NameOccurrence,
    Reference,
    ReferenceKind,
    ReferenceRole,
    RenameEdit,
    TypeHierarchy,
    TypeHierarchyItem,
    WorkspaceEdit,
)
from tyo3.models.symbols import Symbol, SymbolKind
from tyo3.models.editor import (
    FoldingRange,
    FoldingRangeKind,
)

__all__ = [  # noqa: F405
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
    # Rename
    "RenameEdit",
    "WorkspaceEdit",
    # Editor
    "FoldingRangeKind",
    "FoldingRange",
]
