"""Advanced semantic analysis domain models: semantic tokens, type hierarchy.

Derived from tyo3-advanced.allium

> These are spec-anticipation models with no backend implementation yet.
> ``SemanticToken.file`` uses ``PurePosixPath`` instead of ``ProjectFile``
> until the project-file relationship has a backend source.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from tyo3.models.analysis import Range

# ── Enums ────────────────────────────────────────────────────────────────


class SemanticTokenType(StrEnum):
    """Classification of a semantic token."""

    NAMESPACE = "namespace"
    CLASS_ = "class_"
    PARAMETER = "parameter"
    SELF_PARAMETER = "self_parameter"
    CLS_PARAMETER = "cls_parameter"
    VARIABLE = "variable"
    PROPERTY = "property"
    FUNCTION = "function"
    METHOD = "method"
    KEYWORD = "keyword"
    STRING = "string"
    NUMBER = "number"
    DECORATOR = "decorator"
    BUILTIN_CONSTANT = "builtin_constant"
    TYPE_PARAMETER = "type_parameter"


class SemanticTokenModifier(StrEnum):
    """Modifier attribute for a semantic token."""

    DEFINITION = "definition"
    READONLY = "readonly"
    ASYNC_ = "async_"
    DOCUMENTATION = "documentation"


# ── Entities ─────────────────────────────────────────────────────────────


class SemanticToken(BaseModel):
    """A classified semantic token within a project file."""

    file: PurePosixPath
    range: Range
    token_type: SemanticTokenType
    modifiers: set[SemanticTokenModifier] = Field(default_factory=set)

__all__ = [
    "SemanticTokenType",
    "SemanticTokenModifier",
    "SemanticToken",
]
