"""Native extension import + typed exception shims.

Split from ``session.py`` (Phase 13). Owns the PyO3 ``_native_impl`` handle and
the typed native exception classes the read/write wrappers catch.
"""

from __future__ import annotations

# ── Native extension import ──────────────────────────────────────────────
#
# The PyO3 extension module — must match
# #[pyo3(name = "_native_impl")] in rust/src/lib.rs
# The .so is placed inside the tyo3 Python package at src/tyo3/_native_impl.cpython-*.so

try:
    from tyo3 import _native_impl as _native
except ImportError:
    _native = None  # type: ignore[assignment]

# Import typed exception classes so we can catch Rust errors without string matching.
try:
    from tyo3._native_impl import (
        ConfigError as _NativeConfigError,
    )
    from tyo3._native_impl import (
        FormatVersionError as _NativeFormatVersionError,
    )
    from tyo3._native_impl import (
        PathResolutionError as _NativePathError,
    )
    from tyo3._native_impl import (
        PositionError as _NativePositionError,
    )
    from tyo3._native_impl import (
        ProjectClosedError as _NativeClosedError,
    )
    from tyo3._native_impl import (
        RevisionEvictedError as _NativeRevisionEvictedError,
    )
except ImportError:
    # When the native extension isn't built, define dummy classes
    # that never match in `except` clauses.
    class _NativeClosedError(Exception):  # type: ignore[no-redef]
        pass

    class _NativePathError(Exception):  # type: ignore[no-redef]
        pass

    class _NativePositionError(Exception):  # type: ignore[no-redef]
        pass

    class _NativeRevisionEvictedError(Exception):  # type: ignore[no-redef]
        pass

    class _NativeConfigError(Exception):  # type: ignore[no-redef]
        pass

    class _NativeFormatVersionError(Exception):  # type: ignore[no-redef]
        pass
