"""Symbol domain models: symbol discovery operations.

Derived from tyo3-symbols.allium
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from tyo3.models.analysis import FileRange, Range
from tyo3.models.core import TyProject


# ── Enums ────────────────────────────────────────────────────────────────


class SymbolKind(StrEnum):
    """Classification of a code symbol."""
    MODULE = "module"
    CLASS_ = "class_"
    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    VARIABLE = "variable"
    CONSTANT = "constant"
    FIELD = "field"
    PARAMETER = "parameter"
    PROPERTY = "property"
    TYPE_PARAMETER = "type_parameter"
    IMPORT_ = "import_"
    UNKNOWN = "unknown"


# ── Entities ─────────────────────────────────────────────────────────────


class Symbol(BaseModel):
    """A code symbol discovered in a TyO3 project."""
    project: TyProject
    name: str
    qualified_name: str | None = None
    kind: SymbolKind
    location: FileRange
    selection_range: Range | None = None
    container_name: str | None = None
    deprecated: bool = False
