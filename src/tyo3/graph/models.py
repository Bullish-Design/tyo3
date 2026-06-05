"""Data models for graph node and edge payloads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


@dataclass(frozen=True, slots=True)
class EdgeDiff:
    """One edge-level graph diff entry."""

    source_id: str
    target_id: str
    data: EdgeData


@dataclass(frozen=True, slots=True)
class GraphDiff:
    """Structural difference between two CodeGraph revisions."""

    added_nodes: list[SymbolNode]
    removed_nodes: list[SymbolNode]
    added_edges: list[EdgeDiff]
    removed_edges: list[EdgeDiff]

    @property
    def changed(self) -> bool:
        return bool(self.added_nodes or self.removed_nodes or self.added_edges or self.removed_edges)


# ── Build report models ───────────────────────────────────────


class GraphBuildFailure(BaseModel):
    """A single failure recorded during graph construction."""

    file: str
    phase: Literal["symbols", "references", "diagnostics", "inheritance"]
    error_type: str
    message: str


class GraphBuildReport(BaseModel):
    """Report produced during graph construction.

    Callers can inspect ``complete`` to determine whether the graph
    was built without errors, or examine ``failures`` for details
    about what went wrong.
    """

    files_indexed: int = 0
    files_total: int = 0
    failures: list[GraphBuildFailure] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failures
