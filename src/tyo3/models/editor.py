"""Editor support models: selection ranges, folding ranges, inlay hints, etc."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from tyo3.models.analysis import Range


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


__all__ = [
    "FoldingRangeKind",
    "FoldingRange",
]
