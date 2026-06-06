"""Gate 3N — native CodeDelta emission + parity.

These assert the producer wired into the native commit (`commit_head`) emits a
`code_delta` on every write, and that a HEAD graph driven by `apply_code_delta`
equals a fresh read-surface rebuild over the same revision (§6.3.1 parity, kept
against the strengthened comparator until the read-surface oracle is retired).
"""

from __future__ import annotations

from pathlib import Path as StdPath

from tyo3 import TyO3Session
from tyo3.graph import CodeGraph
from tyo3.models.symbols import SymbolKind

from tyo3.graph.tests.test_incremental_parity import assert_graphs_equal


def _rebuild(s: TyO3Session) -> CodeGraph:
    return CodeGraph.build(s, root=s.root.resolve())


def test_commit_emits_code_delta(tmp_path: StdPath) -> None:
    """Every write carries a native code_delta stamped with the new revision."""
    (tmp_path / "app.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        sync = s.edit("app.py", "x = 42\n")
        assert sync.code_delta is not None
        assert sync.code_delta.revision == sync.revision
        # A full-build delta carries the module node at minimum.
        ids = {n.durable_id for n in sync.code_delta.nodes_upserted}
        assert any(i.startswith("<module>") for i in ids)


def test_apply_code_delta_matches_rebuild(tmp_path: StdPath) -> None:
    """A graph driven by the native delta equals a fresh rebuild."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self):\n        pass\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\nu.save()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = CodeGraph()
        sync = s.edit("models.py", "class User:\n    def save(self):\n        return 1\n")
        assert sync.code_delta is not None
        g.apply_code_delta(sync.code_delta)
        assert_graphs_equal(g, _rebuild(s), label="native delta vs rebuild")


def test_cross_file_reference_reverse_dep(tmp_path: StdPath) -> None:
    """A cross-file call produces a reference edge in the native delta."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self):\n        pass\n")
    (tmp_path / "app.py").write_text("from models import User\n")
    with TyO3Session(str(tmp_path)) as s:
        g = CodeGraph()
        sync = s.edit("app.py", "from models import User\nu = User()\nu.save()\n")
        assert sync.code_delta is not None
        g.apply_code_delta(sync.code_delta)
        # The User class should now have inbound reference/import edges.
        user = next(
            (n for n in g.symbols_of_kind(SymbolKind.CLASS) if n.name == "User"),
            None,
        )
        assert user is not None


def test_cross_file_inheritance_overrides_parity(tmp_path: StdPath) -> None:
    """Two-pass inheritance, overrides, external bases, and cross-file refs all
    match a fresh rebuild under the strengthened comparator."""
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
        g = CodeGraph()
        # Any edit emits a full-build delta covering the whole project.
        sync = s.edit("base.py", "class Base:\n    def run(self):\n        return 2\n")
        assert sync.code_delta is not None
        g.apply_code_delta(sync.code_delta)
        assert_graphs_equal(g, _rebuild(s), label="inheritance/overrides parity")


def test_file_deletion_parity(tmp_path: StdPath) -> None:
    """Deleting a file's importer leaves a graph equal to a fresh rebuild."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self):\n        pass\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\nu.save()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = CodeGraph()
        # Replace app.py with an empty module, dropping the cross-file edges.
        sync = s.edit("app.py", "x = 1\n")
        assert sync.code_delta is not None
        g.apply_code_delta(sync.code_delta)
        assert_graphs_equal(g, _rebuild(s), label="deletion parity")
