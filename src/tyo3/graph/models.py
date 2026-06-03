"""Data models for graph node and edge payloads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from tyo3.models.analysis import Range
from tyo3.models.navigation import ReferenceRole
from tyo3.models.symbols import SymbolKind


# ── Node payload (Pydantic — consistency + serialization) ──


class SymbolNode(BaseModel):
    """A symbol in the code graph. Stored as a RustworkX node payload."""

    model_config = ConfigDict(frozen=True)

    symbol_id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    file: str
    range: Range
    selection_range: Range | None = None
    documentation: str | None = None
    signature: str | None = None
    external: bool = False
    package: str | None = None


# ── Edge payload (dataclass — construction speed) ──


class EdgeKind(StrEnum):
    """Classification of a relationship between two symbols."""

    DEFINES = "defines"
    CONTAINS = "contains"
    REFERENCES = "references"
    IMPORTS = "imports"
    INHERITS = "inherits"
    OVERRIDES = "overrides"
    TYPE_OF = "type_of"
    RETURNS = "returns"
    INSTANTIATES = "instantiates"


@dataclass(frozen=True, slots=True)
class EdgeData:
    """Payload stored on every graph edge."""

    kind: EdgeKind
    file: str | None = None
    range: Range | None = None
    role: ReferenceRole | None = None
