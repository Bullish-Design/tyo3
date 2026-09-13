"""Final invariant tests — reads do not write (→ Phase 4).

Encodes §5.3 / §5.9: a read accessor must never advance the revision.  Today
the ``graph`` property primes identity by calling ``sync_all()`` (a write), so
reading ``session.graph`` can change ``session.head``.  Phase 4 removes the
read-side write: identity is reconciled at open and on every commit, so a graph
read never mutates the session.

Phase 4 landed the cutover: ``session.graph`` is now a pure projection of the
native code delta (no identity priming, no ``sync_all``), the floating
``latest`` view inherits that side-effect-free build, and ``snapshot().graph()``
builds over the snapshot's own frozen database.  All three reads leave
``session.head`` unchanged — the markers are removed and the asserts are live.
"""

from __future__ import annotations

from pathlib import Path

from tyo3 import TyO3Session


def _open(root: Path, files: dict[str, str]) -> TyO3Session:
    (root / "pyproject.toml").write_text("[project]\nname = 'noread'\nversion = '0.1.0'\n")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    # Deliberately do NOT call sync_all(): a clean open must already have
    # identity reconciled (Phase 1.3), so the first read must not need a write.
    return TyO3Session(str(root))


_FILES = {
    "models.py": "class User:\n    def save(self):\n        return 1\n",
    "main.py": "from models import User\n\ndef run():\n    return User().save()\n",
}


def test_reading_session_graph_does_not_advance_head(tmp_path):
    s = _open(tmp_path, _FILES)
    try:
        before = s.head
        _ = s.graph  # build the live HEAD graph
        after = s.head
        assert after == before, f"reading session.graph advanced head {before} -> {after}"
    finally:
        s.close()


# Now a live assertion (no longer xfail): Phase 1.3 reconciles identity at open,
# so building a snapshot's pinned graph no longer primes identity via a session
# write — snapshot().graph() does not advance head. (The other two read-side-write
# paths in this file remain xfail until Phase 4.)
def test_snapshot_graph_does_not_advance_head(tmp_path):
    s = _open(tmp_path, _FILES)
    try:
        before = s.head
        snap = s.snapshot()
        try:
            snap.graph()  # build the pinned graph
        finally:
            snap.close()
        after = s.head
        assert after == before, f"snapshot().graph() advanced head {before} -> {after}"
    finally:
        s.close()


def test_latest_check_does_not_advance_head(tmp_path):
    s = _open(tmp_path, _FILES)
    try:
        before = s.head
        s.latest.check()
        # A graph read via the floating view must also be side-effect free.
        s.latest.graph()
        after = s.head
        assert after == before, f"latest read advanced head {before} -> {after}"
    finally:
        s.close()
