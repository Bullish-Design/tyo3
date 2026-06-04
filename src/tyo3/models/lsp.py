"""LSP support models: signature help, completion, etc."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CompletionKind(StrEnum):
    """Kind of completion suggestion."""

    TEXT = "text"
    METHOD = "method"
    FUNCTION = "function"
    CONSTRUCTOR = "constructor"
    FIELD = "field"
    VARIABLE = "variable"
    CLASS = "class"
    INTERFACE = "interface"
    MODULE = "module"
    PROPERTY = "property"
    UNIT = "unit"
    VALUE = "value"
    ENUM = "enum"
    KEYWORD = "keyword"
    SNIPPET = "snippet"
    COLOR = "color"
    FILE = "file"
    REFERENCE = "reference"
    FOLDER = "folder"
    ENUM_MEMBER = "enum_member"
    CONSTANT = "constant"
    STRUCT = "struct"
    EVENT = "event"
    OPERATOR = "operator"
    TYPE_PARAMETER = "type_parameter"


class Completion(BaseModel):
    """A single completion suggestion."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    qualified_name: str | None = None
    insert_text: str | None = None
    type_: str | None = None
    kind: CompletionKind | None = None
    module_name: str | None = None


class Parameter(BaseModel):
    """Information about a single parameter in a signature."""

    model_config = ConfigDict(from_attributes=True)

    label: str
    documentation: str | None = None


class Signature(BaseModel):
    """A single function signature."""

    model_config = ConfigDict(from_attributes=True)

    label: str
    documentation: str | None = None
    parameters: list[Parameter] = []
    active_parameter: int | None = None


class SignatureHelp(BaseModel):
    """Signature help information for function calls."""

    model_config = ConfigDict(from_attributes=True)

    signatures: list[Signature] = []
    active_signature: int | None = None


__all__ = [
    "CompletionKind",
    "Completion",
    "Parameter",
    "Signature",
    "SignatureHelp",
]
