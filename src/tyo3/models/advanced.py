"""Advanced semantic analysis domain models: semantic tokens, type hierarchy.

Derived from tyo3-advanced.allium
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from tyo3.models.analysis import Range
from tyo3.models.core import ProjectFile, TyProject


# ── Enums ────────────────────────────────────────────────────────────────


class SemanticTokenType(str):
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


class SemanticTokenModifier(str):
    """Modifier attribute for a semantic token."""
    DEFINITION = "definition"
    READONLY = "readonly"
    ASYNC_ = "async_"
    DOCUMENTATION = "documentation"


# ── Entities ─────────────────────────────────────────────────────────────


class SemanticToken(BaseModel):
    """A classified semantic token within a project file."""
    project: TyProject
    file: ProjectFile
    range: Range
    token_type: str  # SemanticTokenType
    modifiers: set[str] = Field(default_factory=set)  # set of SemanticTokenModifier
