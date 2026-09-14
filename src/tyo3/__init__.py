"""TyO3: Python semantic engine powered by ty/Ruff."""

from __future__ import annotations

__version__ = "0.7.0"

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
    "build_profile",
    "require_release_build",
]

# Try to import the native TyProject class from the compiled Rust extension.
# When the native extension is not built (pure-Python testing), this fails
# silently and TyProject is not available at the package level.
try:
    from tyo3._native_impl import TyProject as _NativeTyProject  # noqa: F401

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False


def build_profile() -> str:
    """Return the profile of the loaded native extension.

    The result is ``"debug"``, ``"release"``, or ``"unknown"`` when the
    native extension is absent. Debug builds leave the local ``tyo3`` crate at
    ``opt-level = 0`` and run the commit path roughly 6x slower. Never quote a
    performance number taken from one.
    """
    try:
        from tyo3._native_impl import __build_profile__

        return __build_profile__
    except ImportError:
        return "unknown"


def require_release_build() -> None:
    """Raise unless the loaded native extension is a release build.

    Call this at the top of any benchmark or performance assertion.
    """
    profile = build_profile()
    if profile != "release":
        raise RuntimeError(
            f"this measurement needs a release build; loaded profile is "
            f"'{profile}'. Run: devenv shell -- build-release"
        )
