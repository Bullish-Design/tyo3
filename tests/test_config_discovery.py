"""Python-level test proving that ty project configuration is discovered and applied.

Phase 2 of the MVCC substrate refactor rewired PyTyProject::open to use
``ProjectMetadata::discover`` + ``apply_configuration_files`` instead of
fabricating a blank ``ProjectMetadata``. This test proves the behavioural
win end-to-end through the public Python API.
"""

from __future__ import annotations

import textwrap
from pathlib import Path as StdPath

import pytest

try:
    from tyo3 import TyO3Session

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")


@needs_native
def test_pyproject_environment_python_version_is_honored(tmp_path: StdPath) -> None:
    """Setting ``python-version`` in pyproject.toml affects type checking.

    PEP 695 generic type parameter syntax (``def foo[T](...)``) is valid
    Python 3.12+.  Forcing ``python-version = "3.8"`` should cause ty to
    report a syntax error that does not appear under the default (system
    Python 3.13) interpretation.
    """
    pep695 = "def foo[T](x: T) -> T: return x\n"
    (tmp_path / "a.py").write_text(pep695)

    # Default: no config → system Python 3.13 → no syntax error.
    session_default = TyO3Session(str(tmp_path))
    result_default = session_default.check()
    session_default.close()

    # Config forces Python 3.8 → syntax error on PEP 695 generics.
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent("""\
            [tool.ty.environment]
            python-version = "3.8"
        """)
    )
    session_38 = TyO3Session(str(tmp_path))
    result_38 = session_38.check()
    session_38.close()

    assert len(result_38.diagnostics) > len(result_default.diagnostics), (
        f"Forcing python-version=3.8 must increase diagnostics for 3.12+ syntax "
        f"(default={len(result_default.diagnostics)}, "
        f"forced_3_8={len(result_38.diagnostics)}) — "
        f"proves discovery+apply_configuration_files ran"
    )
