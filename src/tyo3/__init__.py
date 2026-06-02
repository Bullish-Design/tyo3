"""TyO3: Python semantic engine powered by ty/Ruff."""

from __future__ import annotations

__version__ = "0.1.0"

from tyo3.session import TyO3Session

__all__ = ["TyO3Session"]

# Try to import the native TyProject class from the compiled Rust extension.
# When the native extension is not built (pure-Python testing), this fails
# silently and TyProject is not available at the package level.
try:
    from tyo3._native_impl import TyProject as _NativeTyProject  # noqa: F401
    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False
