"""Identity reads must agree across every read handle.

``id_for`` / ``locate`` / ``needs_review`` / ``orphaned`` used to exist twice:
a strict implementation on ``TyO3Session`` and a copy on ``_ReadOps`` that
swallowed every exception and returned ``None``. Because the native snapshot
handle exposed no identity methods at all, the second one made
``snapshot.id_for(...)`` answer ``None`` unconditionally — silently, forever.

These tests pin the contract that replaced it: one implementation, every handle
answers, and a snapshot answers for **its own** revision.
"""

import tempfile
from pathlib import Path

import pytest

from tyo3 import TyO3Session
from tyo3.exceptions import PositionError, ProjectClosedError

SRC = "class User:\n    def save(self):\n        return 1\n"


def _session(root: Path) -> TyO3Session:
    (root / "pyproject.toml").write_text("[project]\nname = 'test'\nversion = '0.1.0'\n")
    (root / "models.py").write_text(SRC)
    s = TyO3Session(str(root))
    s.sync_all()
    return s


def test_snapshot_id_for_resolves_instead_of_silently_none():
    """The regression: a snapshot must resolve a real id, not always None."""
    with tempfile.TemporaryDirectory() as d:
        with _session(Path(d)) as s:
            snap = s.snapshot()
            assert snap.id_for("models.py", 2, 9) is not None


def test_every_read_handle_agrees_at_the_same_revision():
    """Session, snapshot, and the floating latest view answer identically."""
    with tempfile.TemporaryDirectory() as d:
        with _session(Path(d)) as s:
            did = s.id_for("models.py", 2, 9)
            assert did is not None
            for handle in (s.snapshot(), s.latest):
                assert handle.id_for("models.py", 2, 9) == did
                assert handle.locate(did) == s.locate(did)
                assert handle.needs_review() == s.needs_review()
                assert handle.orphaned() == s.orphaned()


def test_snapshot_identity_is_pinned_not_live():
    """A snapshot reports its own revision, so it survives a later move."""
    with tempfile.TemporaryDirectory() as d:
        with _session(Path(d)) as s:
            did = s.id_for("models.py", 2, 9)
            before = s.snapshot()
            pinned = before.locate(did)
            assert pinned is not None

            # Move save() into a new enclosing class; head follows, snapshot does not.
            s.edit("models.py", "class Account:\n    def save(self):\n        return 1\n")

            assert before.locate(did) == pinned, "pinned snapshot must not follow head"

            after = s.snapshot()
            assert after.locate(did) == s.locate(did), "a fresh snapshot tracks the new head"


def test_identity_errors_are_typed_on_every_handle():
    """A bad position raises, on the session and on a snapshot alike."""
    with tempfile.TemporaryDirectory() as d:
        with _session(Path(d)) as s:
            for handle in (s, s.snapshot(), s.latest):
                with pytest.raises(PositionError):
                    handle.id_for("models.py", 9999, 1)


def test_identity_raises_after_close():
    with tempfile.TemporaryDirectory() as d:
        s = _session(Path(d))
        s.close()
        with pytest.raises(ProjectClosedError):
            s.id_for("models.py", 2, 9)
        with pytest.raises(ProjectClosedError):
            s.needs_review()
