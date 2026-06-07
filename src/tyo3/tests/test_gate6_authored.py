"""Gate 6 — Authored Layers acceptance tests.

Step 0: Pin the API with failing acceptance tests FIRST.
Tests that define the four key behaviours:
  1. Durable write + read
  2. Snapshot isolation + time-travel
  3. needs_review on change, not dropped
  4. Orphaned on delete, not dropped
"""

from __future__ import annotations

import json

import pytest


# ── Step 0: Failing acceptance tests (API does not exist yet) ────────────


@pytest.mark.xfail(reason="Gate 6 Step 0: authored API does not exist yet — pinned for implementation")
def test_durable_write_and_read(tmp_path):
    """Durable write + read.

    session.author("intent", id, value) returns a new revision (r1 > head_before);
    session.authored("intent", id).value == value.
    """
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[hashing.profiles.structure]

[layers.intent]
origin           = "authored"
history          = true
review_on_change = true
""")

    with TyO3Session(str(proj)) as session:
        orig_head = session.head
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        payload = {"note": "compat shim"}
        r1_result = session.author("intent", foo_id, payload)
        assert r1_result.revision > orig_head

        authored_val = session.authored("intent", foo_id)
        assert authored_val.value == payload
        assert authored_val.status == "present"


@pytest.mark.xfail(reason="Gate 6 Step 0: authored API does not exist yet — pinned for implementation")
def test_snapshot_isolation_and_time_travel(tmp_path):
    """Snapshot isolation + time-travel.

    Open a snapshot BEFORE the author; after the author, the old snapshot
    shows 'absent' while a fresh snapshot shows 'present'. A snapshot(at=r1)
    round-trips the value at r1.
    """
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[hashing.profiles.structure]

[layers.intent]
origin           = "authored"
history          = true
review_on_change = true
""")

    with TyO3Session(str(proj)) as session:
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        # Snapshot BEFORE author.
        snap0 = session.snapshot()
        snap0_rev = snap0.revision

        # Author a note.
        payload = {"note": "compat shim"}
        r1_result = session.author("intent", foo_id, payload)
        r1 = r1_result.revision

        # Old snapshot sees absent.
        assert snap0.authored("intent", foo_id).status == "absent"
        snap0.close()

        # Fresh snapshot sees present and the value.
        snap_fresh = session.snapshot()
        assert snap_fresh.authored("intent", foo_id).status == "present"
        assert snap_fresh.authored("intent", foo_id).value == payload
        snap_fresh.close()

        # Time-travel snapshot at r1.
        snap_at_r1 = session.snapshot(at=r1)
        assert snap_at_r1.authored("intent", foo_id).value == payload
        assert snap_at_r1.authored("intent", foo_id).status == "present"
        snap_at_r1.close()

        # Time-travel snapshot at snap0's rev (before author) sees absent.
        snap_at_old = session.snapshot(at=snap0_rev)
        assert snap_at_old.authored("intent", foo_id).status == "absent"
        snap_at_old.close()


@pytest.mark.xfail(reason="Gate 6 Step 0: authored API does not exist yet — pinned for implementation")
def test_needs_review_on_change_not_dropped(tmp_path):
    """needs_review on change, not dropped.

    Edit the body of the entity that has an authored note;
    the note's status should be 'needs_review' and the value is still readable.
    """
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[hashing.profiles.structure]

[layers.intent]
origin           = "authored"
history          = true
review_on_change = true
""")

    with TyO3Session(str(proj)) as session:
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        payload = {"note": "compat shim"}
        session.author("intent", foo_id, payload)

        # Before edit, note is present.
        assert session.authored("intent", foo_id).status == "present"

        # Edit the entity's body (code write).
        session.edit("a.py", "def foo():\n    return 99\n")

        # After edit, note should be needs_review but value intact.
        val = session.authored("intent", foo_id)
        assert val.status == "needs_review", f"Expected needs_review, got {val.status}"
        assert val.value == payload


@pytest.mark.xfail(reason="Gate 6 Step 0: authored API does not exist yet — pinned for implementation")
def test_orphaned_on_delete_not_dropped(tmp_path):
    """Orphaned on delete, not dropped.

    Delete the entity that has an authored note; the note's status should be
    'orphaned' and the value is intact.
    """
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[hashing.profiles.structure]

[layers.intent]
origin           = "authored"
history          = true
review_on_change = true
""")

    with TyO3Session(str(proj)) as session:
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        payload = {"note": "compat shim"}
        session.author("intent", foo_id, payload)

        # Delete the entity.
        session.edit("a.py", "")

        # Status should be orphaned, value intact.
        val = session.authored("intent", foo_id)
        assert val.status == "orphaned", f"Expected orphaned, got {val.status}"
        assert val.value == payload
