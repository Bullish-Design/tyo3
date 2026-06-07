"""Gate 8 — Delta Subscription Bus acceptance tests.

Step 0: Pin the bus API with failing tests FIRST.
Tests that define the key behaviours:
  1. Scoped, reverse-dep-aware delivery
  2. Revision-stamped read-at-R
  3. Clean teardown

These tests are expected to fail (xfail) until Step 5 wires the bus
into the write path.  They exist to fix the contract.
"""

from __future__ import annotations

import pytest

# ── Step 0: Failing acceptance tests (API does not exist yet) ────────────


def test_scoped_reverse_dep_delivery(tmp_path):
    """Scoped, reverse-dep-aware delivery (§12.2.1, §4.3.3).

    A subscriber interested in ``models.py`` is notified on edits to it
    and to its importers (reverse-dep), and not on unrelated edits.
    """
    from tyo3 import TyO3Session
    from tyo3.bus.interest import Interest

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "models.py").write_text("class User:\n    name: str\n")
    (proj / "app.py").write_text("from models import User\n\ndef create():\n    return User()\n")
    (proj / "other.py").write_text("def unrelated():\n    pass\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"

[coordination.watcher]
enabled = false
debounce_ms = 200
""")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        user_id = session.id_for("models.py", 1, 7)  # class User
        assert user_id is not None

        sub = session.subscribe(Interest.files({"models.py"}))

        # Edit models.py → subscriber is notified
        result1 = session.edit("models.py", "class User:\n    name: str\n    age: int\n")
        delta = sub.poll(timeout=2.0)
        assert delta is not None, "subscriber should be notified on models.py edit"
        assert delta.revision == result1.revision
        assert user_id in delta.changed

        # Edit the importer (app.py imports models.py) → subscriber IS notified (reverse-dep)
        result2 = session.edit("app.py", "from models import User\n\ndef create():\n    return User(name=\"Alice\")\n")
        delta2 = sub.poll(timeout=2.0)
        assert delta2 is not None, "subscriber should be notified on importer edit (reverse-dep)"

        # Edit an unrelated file → subscriber NOT notified
        result3 = session.edit("other.py", "def unrelated():\n    return 42\n")
        delta3 = sub.poll(timeout=0.5)
        assert delta3 is None, "subscriber should NOT be notified on unrelated edit"

        sub.close()


def test_revision_stamped_read_at_r(tmp_path):
    """Revision-stamped read-at-R (§12.2.3).

    For a delivered delta, ``session.snapshot(at=delta.revision)`` reads
    exactly the notified state (Gate 7).
    """
    from tyo3 import TyO3Session
    from tyo3.bus.interest import Interest

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "models.py").write_text("class User:\n    name: str\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"
""")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        user_id = session.id_for("models.py", 1, 7)
        assert user_id is not None

        sub = session.subscribe(Interest.ALL)

        result = session.edit("models.py", "class User:\n    name: str\n    age: int\n")
        delta = sub.poll(timeout=2.0)
        assert delta is not None
        assert delta.revision == result.revision

        # Read at the notified revision
        with session.snapshot(at=delta.revision) as snap:
            ev = snap.entity(user_id)
            assert ev is not None
            assert ev.revision == delta.revision

        sub.close()


def test_clean_teardown(tmp_path):
    """Clean teardown (§12.2.6).

    ``sub.close()`` (or ``with session.subscribe(...) as sub:``) stops
    delivery; a later edit is not enqueued to it.
    """
    from tyo3 import TyO3Session
    from tyo3.bus.interest import Interest

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "models.py").write_text("class User:\n    name: str\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"
""")

    with TyO3Session(str(proj)) as session:
        session.sync_all()

        sub = session.subscribe(Interest.ALL)

        # Edit before close → notified
        session.edit("models.py", "class User:\n    name: str\n    age: int\n")
        delta1 = sub.poll(timeout=2.0)
        assert delta1 is not None

        sub.close()

        # Edit after close → NOT enqueued
        session.edit("models.py", "class User:\n    name: str\n    age: int\n    active: bool\n")
        delta2 = sub.poll(timeout=0.5)
        assert delta2 is None

        # Also test context-manager form
        with session.subscribe(Interest.ALL) as sub2:
            session.edit("models.py", "class User:\n    name: str\n")
            delta3 = sub2.poll(timeout=2.0)
            assert delta3 is not None

        # After context exit, delivery stops
        # (sub2 is already closed by __exit__)
