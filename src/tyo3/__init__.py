"""TyO3: Python semantic engine powered by ty/Ruff."""

from __future__ import annotations

__version__ = "0.4.0"

from tyo3.graph import CodeGraph
from tyo3.models.analysis import CheckResult, Diagnostic
from tyo3.models.navigation import DefinitionTarget, HoverResult, Reference
from tyo3.models.symbols import Symbol
from tyo3.session import Snapshot, TyO3Session

__all__ = [
    "TyO3Session",
    "Snapshot",
    # Re-export commonly used types for convenience
    "CheckResult",
    "Diagnostic",
    "Symbol",
    "DefinitionTarget",
    "Reference",
    "HoverResult",
    # Graph
    "CodeGraph",
]

# Try to import the native TyProject class from the compiled Rust extension.
# When the native extension is not built (pure-Python testing), this fails
# silently and TyProject is not available at the package level.
try:
    from tyo3._native_impl import TyProject as _NativeTyProject  # noqa: F401

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False
