"""Navigation domain models: goto definition, references, hover.

Derived from tyo3-navigation.allium
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict

from tyo3.models.analysis import FileRange, Range
from tyo3.models.symbols import Symbol

# ── Enums ────────────────────────────────────────────────────────────────


class ReferenceKind(StrEnum):
    """Classification of a reference occurrence."""

    READ = "read"
    WRITE = "write"
    OTHER = "other"


class ReferenceRole(StrEnum):
    """How a symbol is used at a reference site."""

    READ = "read"
    WRITE = "write"
    IMPORT = "import"
    DEFINITION = "definition"
    OTHER = "other"


class NameOccurrence(BaseModel):
    """A single resolved name occurrence in a file.

    Records where a name appears, what symbol it resolves to, and
    the reference role (read, write, import, or definition).
    Produced by :meth:`TyO3Session.file_occurrences`.
    """

    model_config = ConfigDict(from_attributes=True)

    range: Range
    target_file: str | None = None
    target_name: str | None = None
    target_qualified_name: str | None = None
    role: ReferenceRole


class HoverContentKind(StrEnum):
    """Kind of hover content returned by the ty engine."""

    TYPE = "type"
    SIGNATURE = "signature"
    DOCSTRING = "docstring"
    TYPED_DICT_KEY = "typed_dict_key"
    MARKDOWN = "markdown"
    PLAIN_TEXT = "plain_text"


# ── Entities ─────────────────────────────────────────────────────────────


class DefinitionTarget(BaseModel):
    """A navigation target produced by goto-definition-like operations."""

    model_config = ConfigDict(from_attributes=True)

    path: PurePosixPath
    range: Range
    selection_range: Range | None = None
    symbol: Symbol | None = None
    module_name: str | None = None


class Reference(BaseModel):
    """A reference occurrence within a project file."""

    model_config = ConfigDict(from_attributes=True)

    path: PurePosixPath
    range: Range
    kind: ReferenceKind


class HoverContent(BaseModel):
    """A single piece of structured hover information."""

    model_config = ConfigDict(from_attributes=True)

    kind: HoverContentKind
    value: str


class HoverResult(BaseModel):
    """Structured hover information for a symbol location."""

    model_config = ConfigDict(from_attributes=True)

    location: FileRange
    contents: list[HoverContent]


class TypeHierarchyItem(BaseModel):
    """An item in a type hierarchy (class with its supertypes/subtypes)."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    detail: str | None = None
    path: PurePosixPath
    full_range: Range
    selection_range: Range


class TypeHierarchy(BaseModel):
    """Result of a type hierarchy query."""

    model_config = ConfigDict(from_attributes=True)

    item: TypeHierarchyItem
    supertypes: list[TypeHierarchyItem] = []
    subtypes: list[TypeHierarchyItem] = []


__all__ = [
    "ReferenceKind",
    "ReferenceRole",
    "NameOccurrence",
    "HoverContentKind",
    "DefinitionTarget",
    "Reference",
    "HoverContent",
    "HoverResult",
    "TypeHierarchyItem",
    "TypeHierarchy",
]
