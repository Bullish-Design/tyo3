"""Gate 3N — native CodeDelta emission + parity.

The native commit (`commit_head`) emits a `code_delta` on every write: a full
`rescan` delta on rescan commits, a bounded incremental diff otherwise. These
tests assert that a replica seeded from the native full delta and then driven by
the per-commit incremental deltas equals a fresh native full build at the same
revision (§6.3.1 parity, under the strengthened comparator).

A separate check confirms the native full producer still matches the legacy
read-surface build, guarding the Step 5 producer refactor until the read-surface
oracle is retired in Step 7.
"""

from __future__ import annotations

from pathlib import Path as StdPath

from tyo3 import TyO3Session
from tyo3.graph import CodeGraph
from tyo3.models.analysis import CodeDelta
from tyo3.models.symbols import SymbolKind

from tyo3.graph.tests.test_incremental_parity import assert_graphs_equal


def _native_full(s: TyO3Session) -> CodeGraph:
    """A replica built by applying the native full (`rescan`) CodeDelta — the
    Step 7 cold-start path, used here as the parity oracle."""
    g = CodeGraph()
    delta = CodeDelta.model_validate(s._inner.code_delta_full())
    g.apply_code_delta(delta)
    return g


def test_commit_emits_code_delta(tmp_path: StdPath) -> None:
    """Every write carries a native code_delta stamped with the new revision."""
    (tmp_path / "app.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        sync = s.edit("app.py", "x = 42\n")
        assert sync.code_delta is not None
        assert sync.code_delta.revision == sync.revision
        # A content change to `x` re-hashes that node → it is upserted.
        assert not sync.code_delta.rescan
        assert sync.code_delta.nodes_upserted, "expected the changed node upserted"


def test_native_full_matches_read_surface(tmp_path: StdPath) -> None:
    """The native full producer equals the legacy read-surface build (guards the
    Step 5 producer refactor; removed when the oracle is retired in Step 7)."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self):\n        pass\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\nu.save()\n")
    with TyO3Session(str(tmp_path)) as s:
        s.sync_all()
        s._graph_identity_primed = True
        assert_graphs_equal(
            _native_full(s),
            CodeGraph.build(s),
            label="native full vs read-surface",
        )


def test_incremental_matches_full_after_edit(tmp_path: StdPath) -> None:
    """A replica driven by the incremental delta equals a fresh native full build."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self):\n        pass\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\nu.save()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _native_full(s)
        sync = s.edit("models.py", "class User:\n    def save(self):\n        return 1\n")
        assert sync.code_delta is not None and not sync.code_delta.rescan
        g.apply_code_delta(sync.code_delta)
        assert_graphs_equal(g, _native_full(s), label="incremental vs full")


def test_cross_file_reference_added(tmp_path: StdPath) -> None:
    """Adding a cross-file call resolves a reference edge in the incremental delta."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self):\n        pass\n")
    (tmp_path / "app.py").write_text("from models import User\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _native_full(s)
        sync = s.edit("app.py", "from models import User\nu = User()\nu.save()\n")
        assert sync.code_delta is not None and not sync.code_delta.rescan
        g.apply_code_delta(sync.code_delta)
        assert_graphs_equal(g, _native_full(s), label="cross-file ref added")
        user = next(
            (n for n in g.symbols_of_kind(SymbolKind.CLASS) if n.name == "User"),
            None,
        )
        assert user is not None


def test_cross_file_inheritance_overrides_parity(tmp_path: StdPath) -> None:
    """Two-pass inheritance, overrides, external bases, and cross-file refs all
    match a fresh full build after an incremental edit."""
    (tmp_path / "base.py").write_text(
        "class Base:\n    def run(self):\n        return 0\n"
    )
    (tmp_path / "derived.py").write_text(
        "from base import Base\n"
        "class Derived(Base):\n"
        "    def run(self):\n"
        "        return 1\n"
    )
    (tmp_path / "app.py").write_text("from derived import Derived\nd = Derived()\nd.run()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _native_full(s)
        sync = s.edit("base.py", "class Base:\n    def run(self):\n        return 2\n")
        assert sync.code_delta is not None and not sync.code_delta.rescan
        g.apply_code_delta(sync.code_delta)
        assert_graphs_equal(g, _native_full(s), label="inheritance/overrides parity")


def test_file_deletion_parity(tmp_path: StdPath) -> None:
    """Dropping an importer's cross-file edges leaves a graph equal to a full build."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self):\n        pass\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\nu.save()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _native_full(s)
        sync = s.edit("app.py", "x = 1\n")
        assert sync.code_delta is not None and not sync.code_delta.rescan
        g.apply_code_delta(sync.code_delta)
        assert_graphs_equal(g, _native_full(s), label="deletion parity")
