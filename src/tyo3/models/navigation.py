"""Navigation domain models: goto definition, references, hover.

Derived from tyo3-navigation.allium
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional

from pydantic import BaseModel

from tyo3.models.analysis import FileRange, Range
from tyo3.models.core import Path, TyProject
from tyo3.models.symbols import Symbol


# ── Enums ────────────────────────────────────────────────────────────────


class ReferenceKind(str):
    """Classification of a reference occurrence."""
    READ = "read"
    WRITE = "write"
    OTHER = "other"


class HoverContentKind(StrEnum):
    """Kind of hover content returned by the ty engine.

    Maps to :class:`tyo3.rust_backend.HoverContentKindDto` variants.
    """
    TYPE = "type"
    SIGNATURE = "signature"
    DOCSTRING = "docstring"
    TYPED_DICT_KEY = "typed_dict_key"
    MARKDOWN = "markdown"
    PLAIN_TEXT = "plain_text"


# ── Entities ─────────────────────────────────────────────────────────────


class DefinitionTarget(BaseModel):
    """A navigation target produced by goto-definition-like operations."""
    project: TyProject
    path: Path
    range: Range
    selection_range: Optional[Range] = None
    symbol: Optional[Symbol] = None
    module_name: Optional[str] = None


class Reference(BaseModel):
    """A reference occurrence within a project file."""
    project: TyProject
    path: Path
    range: Range
    kind: str  # ReferenceKind


class HoverContent(BaseModel):
    """A single piece of structured hover information."""
    kind: HoverContentKind
    value: str


class HoverResult(BaseModel):
    """Structured hover information for a symbol location."""
    location: FileRange
    contents: list[HoverContent]
