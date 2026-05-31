"""Navigation domain models: goto definition, references, hover.

Derived from tyo3-navigation.allium
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from tyo3.models.analysis import Range
from tyo3.models.core import Path, TyProject
from tyo3.models.symbols import Symbol


# ── Enums ────────────────────────────────────────────────────────────────


class ReferenceKind(str):
    """Classification of a reference occurrence."""
    READ = "read"
    WRITE = "write"
    OTHER = "other"


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
