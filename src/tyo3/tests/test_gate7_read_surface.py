"""Gate 7 — Unified Read Surface acceptance tests.

Step 0: Pin the API with failing tests FIRST.
Tests that define the key behaviours:
  1. Cross-layer join (snap.entity)
  2. Combined diff (a.diff(b))
  3. LatestView warm reads + session convenience sugar
"""

from __future__ import annotations

import pytest

# These test functions use xfail because the API doesn't exist yet.
# They will be un-xfail'd as each step is implemented.


@pytest.mark.xfail(reason="Gate 7 Step 0: API does not exist yet — pinned for implementation")
def test_cross_layer_join_entity_view(tmp_path):
    """Cross-layer join (§10.2.2).

    `snap.entity(id)` exposes code, content_hash, derived, authored,
    status, and location — all at snap.revision.
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

[layers.upper]
origin = "derived"
depends_on = ["code"]
generator = "upper_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_upper"
serving = "stale"
entity_kinds = ["function"]

[layers.intent]
origin           = "authored"
history          = true
review_on_change = true

[generators.upper_gen]
type = "python"
callable = "tyo3.tests.test_gate7_read_surface:uppercase_generator"

[stores.kv_upper]
backend = "fs"
path = "cache/upper"
""")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        # Author a note for foo.
        session.author("intent", foo_id, {"note": "compat shim"})

        # Take a snapshot.
        snap = session.snapshot()
        ev = snap.entity(foo_id)
        assert ev.durable_id == foo_id
        assert ev.revision == snap.revision
        # Code layer
        assert ev.code is not None
        assert ev.code.name == "foo"
        assert ev.content_hash is not None
        assert ev.location is not None
        # Derived
        assert "upper" in ev.derived
        assert ev.derived["upper"].status in ("fresh", "stale")
        # Authored
        assert "intent" in ev.authored
        assert ev.authored["intent"].status == "present"
        assert ev.authored["intent"].value == {"note": "compat shim"}
        # Status
        assert ev.status in ("active", "present")

        snap.close()


@pytest.mark.xfail(reason="Gate 7 Step 0: API does not exist yet — pinned for implementation")
def test_combined_snapshot_diff(tmp_path):
    """Combined diff (§10.3).

    Capture r0 and r1; d = after.diff(before) reports changes across
    code, derived, and authored layers keyed by DurableId.
    """
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\ndef bar():\n    return 2\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[hashing.profiles.structure]

[layers.upper]
origin = "derived"
depends_on = ["code"]
generator = "upper_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_upper"
serving = "stale"
entity_kinds = ["function"]

[layers.intent]
origin           = "authored"
history          = true
review_on_change = true

[generators.upper_gen]
type = "python"
callable = "tyo3.tests.test_gate7_read_surface:uppercase_generator"

[stores.kv_upper]
backend = "fs"
path = "cache/upper"
""")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        bar_id = session.id_for("a.py", 3, 5)
        assert foo_id is not None
        assert bar_id is not None

        # Author a note on bar.
        session.author("intent", bar_id, {"note": "bar helper"})

        # Snapshot at R0 (before edits).
        r0 = session.head
        before = session.snapshot()

        # Edit foo's body.
        session.edit("a.py", "def foo():\n    return 99\ndef bar():\n    return 2\n")
        # Re-author note on bar.
        session.author("intent", bar_id, {"note": "bar helper updated"})
        r1 = session.head

        after = session.snapshot()

        d = after.diff(before)

        # Code diff: foo changed.
        assert foo_id in d.code.changed
        assert bar_id not in d.code.changed

        # Derived diff: foo's artifact drifted.
        assert foo_id in d.derived["upper"].drifted
        assert bar_id not in d.derived["upper"].drifted

        # Authored diff: bar's note changed.
        assert bar_id in d.authored["intent"].changed

        # entities() is the union.
        entities = d.entities()
        assert foo_id in entities
        assert bar_id in entities

        before.close()
        after.close()


@pytest.mark.xfail(reason="Gate 7 Step 0: API does not exist yet — pinned for implementation")
def test_latest_warm_snapshot_consistent(tmp_path):
    """LatestView is warm; Snapshot is consistent; session convenience sugar.

    - session.latest.derived("upper", id) returns a value warm and floats.
    - session.entity(id) is consistent sugar over the held head snapshot.
    - latest does NOT expose entity() or diff() (the honest boundary).
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

[layers.upper]
origin = "derived"
depends_on = ["code"]
generator = "upper_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_upper"
serving = "stale"
entity_kinds = ["function"]

[generators.upper_gen]
type = "python"
callable = "tyo3.tests.test_gate7_read_surface:uppercase_generator"

[stores.kv_upper]
backend = "fs"
path = "cache/upper"
""")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        # LatestView: warm derived read.
        lv = session.latest
        val = lv.derived("upper", foo_id)
        assert val is not None
        assert val.status in ("fresh", "absent")

        # Session entity (sugar over head snapshot).
        ev = session.entity(foo_id)
        assert ev.durable_id == foo_id
        assert ev.code is not None

        # Honest boundary: latest has no entity or diff.
        assert not hasattr(lv, "entity")
        assert not hasattr(lv, "diff")

        # Snapshot pins, latest floats.
        snap = session.snapshot()
        snap_derived = snap.derived("upper", foo_id)
        assert snap_derived is not None

        # Session convenience sugar
        session.code  # should be a CodeLayerView-like
        session.layer("upper")  # should dispatch by config

        # Convenience diff sugar
        snap2 = session.snapshot()
        d = session.diff(snap2)  # diff from held head snapshot
        assert d.is_empty()

        snap.close()
        snap2.close()


# ── Test generator (in-process, deterministic) ────────────────────────────

_UPPERCASE_CALL_COUNT = 0


def uppercase_generator(artifact: bytes) -> bytes:
    """Deterministic in-process generator: returns uppercased artifact bytes."""
    global _UPPERCASE_CALL_COUNT
    _UPPERCASE_CALL_COUNT += 1
    return artifact.upper()


def reset_uppercase_call_count() -> None:
    global _UPPERCASE_CALL_COUNT
    _UPPERCASE_CALL_COUNT = 0
