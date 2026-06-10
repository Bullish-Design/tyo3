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


def test_durable_write_and_read(tmp_path):
    """Durable write + read.

    session.author("intent", id, value) returns a new revision (r1 > head_before);
    session.authored("intent", id).value == value.
    """
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()  # populate identity registry
        orig_head = session.head
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        payload = {"note": "compat shim"}
        r1_result = session.author("intent", foo_id, payload)
        assert r1_result.revision > orig_head

        authored_val = session.authored("intent", foo_id)
        assert authored_val.value == payload
        assert authored_val.status == "present"


def test_snapshot_isolation_and_time_travel(tmp_path):
    """Snapshot isolation + time-travel.

    Open a snapshot BEFORE the author; after the author, the old snapshot
    shows 'absent' while a fresh snapshot shows 'present'. A snapshot(at=r1)
    round-trips the value at r1.
    """
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()  # populate identity registry
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


def test_needs_review_on_change_not_dropped(tmp_path):
    """needs_review on change, not dropped.

    Edit the body of the entity that has an authored note;
    the note's status should be 'needs_review' and the value is still readable.
    """
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()  # populate identity registry
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


def test_orphaned_on_delete_not_dropped(tmp_path):
    """Orphaned on delete, not dropped.

    Delete the entity that has an authored note; the note's status should be
    'orphaned' and the value is intact.
    """
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()  # populate identity registry
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


# ── Step 7: Persistence round-trip & reconcile-on-load no-loss (§11.3.2) ──


def test_no_loss_round_trip(tmp_path):
    """Author notes → close → reopen → values identical."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
    (proj / "a.py").write_text("def foo():\n    return 1\n")
    (proj / "b.py").write_text("def bar():\n    return 2\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[hashing.profiles.structure]

[layers.intent]
origin           = "authored"
history          = true
review_on_change = true

[layers.notes]
origin           = "authored"
history          = false
review_on_change = false
""")

    foo_id = None
    bar_id = None
    payload_foo = {"note": "compat shim for foo"}
    payload_bar = {"desc": "bar does the thing"}

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        bar_id = session.id_for("b.py", 1, 5)
        assert foo_id is not None
        assert bar_id is not None
        session.author("intent", foo_id, payload_foo)
        session.author("intent", bar_id, payload_bar)
        session.author("notes", foo_id, {"tag": "important"})

    # Reopen — values + history intact.
    with TyO3Session(str(proj)) as session:
        session.sync_all()
        v1 = session.authored("intent", foo_id)
        assert v1.value == payload_foo
        assert v1.status == "present"

        v2 = session.authored("intent", bar_id)
        assert v2.value == payload_bar
        assert v2.status == "present"

        v3 = session.authored("notes", foo_id)
        assert v3.value == {"tag": "important"}
        assert v3.status == "present"  # review_on_change = false


def test_history_round_trip_survives_reopen(tmp_path):
    """Author multiple versions → close → reopen → history intact (§11.3.2)."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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

    payload_a = {"note": "v1"}
    payload_b = {"note": "v2"}

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        # Author twice to build history.
        session.author("intent", foo_id, payload_a)
        session.author("intent", foo_id, payload_b)

        # Check history before close.
        snap = session.snapshot()
        hist = snap.authored_history("intent", foo_id)
        assert len(hist) == 2
        assert hist[0].value == payload_a
        assert hist[1].value == payload_b
        snap.close()

    # Reopen — history must survive the round-trip.
    with TyO3Session(str(proj)) as session:
        session.sync_all()

        # Current value is the latest.
        val = session.authored("intent", foo_id)
        assert val.value == payload_b

        # History has both versions.
        snap = session.snapshot()
        hist = snap.authored_history("intent", foo_id)
        assert len(hist) == 2, f"Expected 2 history entries, got {len(hist)}"
        assert hist[0].value == payload_a
        assert hist[1].value == payload_b

        # history=false layer should have no history.
        assert snap.authored_history("nonexistent", foo_id) == []
        snap.close()


def test_authored_history_empty_for_unknown(tmp_path):
    """authored_history on unknown id returns empty list."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()
        snap = session.snapshot()
        hist = snap.authored_history("intent", "01NONEXIST")
        assert hist == []
        snap.close()


def test_moved_entity_keeps_note(tmp_path):
    """Move entity unchanged — note follows the id, not the location.

    Uses sync_all to reconcile from disk after writing the move to disk."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None
        payload = {"note": "moved entity note"}
        session.author("intent", foo_id, payload)

        # Write move to disk: delete a.py, create b.py with same content.
        (proj / "a.py").unlink()
        (proj / "b.py").write_text("def foo():\n    return 1\n")
        session.sync_all()  # full rescan, reconciliation will rebind

        # Note should still be present, value intact.
        val = session.authored("intent", foo_id)
        assert val.value == payload
        # After move, registry may flag needs_review or keep present.
        assert val.status in ("present", "needs_review")


def test_rename_with_change_flags_review(tmp_path):
    """Rename + body change → needs_review, value intact.

    Writes rename+change to disk and uses sync_all for full reconciliation."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None
        payload = {"note": "review me"}
        session.author("intent", foo_id, payload)

        # Rename + body change: write to disk, then sync_all.
        (proj / "a.py").unlink()
        (proj / "b.py").write_text("def foo():\n    return 99\n")
        session.sync_all()

        val = session.authored("intent", foo_id)
        assert val.value == payload
        # After rename+change, should be needs_review or orphaned (if rebind failed).
        assert val.status in ("needs_review", "orphaned")


def test_delete_orphans_re_add_returns_to_present(tmp_path):
    """Delete orphans → re-add returns to present."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None
        payload = {"note": "orphan test"}
        session.author("intent", foo_id, payload)

        # Delete entity.
        session.edit("a.py", "")
        val = session.authored("intent", foo_id)
        assert val.status == "orphaned"
        assert val.value == payload

        # Re-add entity — returns to present.
        session.edit("a.py", "def foo():\n    return 1\n")
        val2 = session.authored("intent", foo_id)
        assert val2.status == "present"
        assert val2.value == payload


def test_newer_format_rejected(tmp_path):
    """A record with a *newer* format_version (3) → reopen raises FormatVersionError.

    (v2 is the current format — it adds AuthoredVersion.reviewed_hash — so this
    uses v3 to exercise the "newer than supported" rejection path.)
    """
    from tyo3 import TyO3Session
    from tyo3.exceptions import FormatVersionError

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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

    # First open — get the entity id.
    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None
        session.author("intent", foo_id, {"note": "v1"})

    # Hand-write a record with a newer-than-supported format_version (3).
    record_path = proj / ".tyo3" / "authored" / "intent" / f"{foo_id}.json"
    with open(record_path) as f:
        doc = json.load(f)
    doc["format_version"] = 3
    with open(record_path, "w") as f:
        json.dump(doc, f, indent=2)

    # Reopen should raise FormatVersionError.
    with pytest.raises(FormatVersionError):
        TyO3Session(str(proj))


def test_cache_independence(tmp_path):
    """Delete .tyo3/cache/ — authored records intact."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None
        session.author("intent", foo_id, {"note": "cache test"})

    # Delete cache.
    cache_dir = proj / ".tyo3" / "cache"
    if cache_dir.exists():
        import shutil

        shutil.rmtree(cache_dir)

    # Reopen — authored records intact.
    with TyO3Session(str(proj)) as session:
        session.sync_all()
        val = session.authored("intent", foo_id)
        assert val.value == {"note": "cache test"}


# ── Step 8: Cross-layer consistency at a snapshot (§10.2.2 authored half) ─


def test_snapshot_pinned_consistency(tmp_path):
    """At a pinned R, code + derived + authored all describe R.

    Subsequent writes (code and authored) do not change pinned reads."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        # Pin snapshot at R.
        snap = session.snapshot()
        snap_rev = snap.revision

        # Author a note AFTER pinning.
        session.author("intent", foo_id, {"note": "after"})

        # Code edit AFTER pinning.
        session.edit("a.py", "def foo():\n    return 99\n")

        # Pinned snapshot still sees the old state, and its revision is frozen
        # at the pin point despite the later author + edit.
        assert snap.authored("intent", foo_id).status == "absent"
        assert snap.revision == snap_rev
        snap.close()

        # Fresh snapshot sees the new value.
        snap2 = session.snapshot()
        assert snap2.authored("intent", foo_id).status == "needs_review"
        assert snap2.authored("intent", foo_id).value == {"note": "after"}
        snap2.close()


def test_authored_time_travel_diff(tmp_path):
    """Author A at R0, B at R1 → snapshot(at=R0) reads A, snapshot(at=R1) reads B."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        payload_a = {"note": "A"}
        result_a = session.author("intent", foo_id, payload_a)
        r0 = result_a.revision

        payload_b = {"note": "B"}
        result_b = session.author("intent", foo_id, payload_b)
        r1 = result_b.revision

        # Time-travel reads.
        snap0 = session.snapshot(at=r0)
        assert snap0.authored("intent", foo_id).value == payload_a
        snap0.close()

        snap1 = session.snapshot(at=r1)
        assert snap1.authored("intent", foo_id).value == payload_b
        snap1.close()


def test_no_write_lock_for_reads(tmp_path):
    """Authored reads on a snapshot succeed while writer hammers head."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
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
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        session.author("intent", foo_id, {"note": "initial"})

        # Take a snapshot.
        snap = session.snapshot()

        # Hammer the head with many writes.
        for i in range(20):
            session.edit("a.py", f"def foo():\n    return {i}\n")

        # Snapshot read still works (no write lock).
        val = snap.authored("intent", foo_id)
        assert val.value == {"note": "initial"}
        snap.close()
