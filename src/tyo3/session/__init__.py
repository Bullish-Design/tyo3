"""TyO3Session — unified project session API.

The single public class wraps the Rust ``TyProject`` PyO3 class with Pydantic
model validation. This package was split from a single ``session.py`` module
(Phase 13) into a thin facade (:mod:`~tyo3.session.session`), the shared
read surface (:mod:`~tyo3.session.read_ops`), the read views
(:mod:`~tyo3.session.views`), and the native-handle shims
(:mod:`~tyo3.session._native`). The public surface is unchanged::

    from tyo3 import TyO3Session

    with TyO3Session("/path/to/project") as session:
        result = session.check()
"""

from __future__ import annotations

from tyo3.session.session import TyO3Session
from tyo3.session.views import LatestView, Snapshot

__all__ = ["LatestView", "Snapshot", "TyO3Session"]
