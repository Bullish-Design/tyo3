"""Editor support models: selection ranges, folding ranges, inlay hints, etc."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from tyo3.models.analysis import Position, Range


class FoldingRangeKind(StrEnum):
    """Kind of folding range."""

    COMMENT = "comment"
    IMPORTS = "imports"
    REGION = "region"


class FoldingRange(BaseModel):
    """A folding range in the source code."""

    model_config = ConfigDict(from_attributes=True)

    range: Range
    kind: FoldingRangeKind | None = None


class InlayHintKind(StrEnum):
    """Kind of inlay hint."""

    TYPE = "type"
    CALL_ARGUMENT_NAME = "call_argument_name"


class InlayHint(BaseModel):
    """An inlay hint to display in the editor."""

    model_config = ConfigDict(from_attributes=True)

    position: Position
    label: str
    kind: InlayHintKind


class HintKind(StrEnum):
    """Kind of hint."""

    UNUSED = "unused"
    UNREACHABLE = "unreachable"


class Hint(BaseModel):
    """A hint about the source code (unused bindings, unreachable code)."""

    model_config = ConfigDict(from_attributes=True)

    message: str
    kind: HintKind
    range: Range | None = None


__all__ = [
    "FoldingRangeKind",
    "FoldingRange",
    "InlayHintKind",
    "InlayHint",
    "HintKind",
    "Hint",
]
