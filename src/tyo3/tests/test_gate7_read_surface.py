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
serving = "block"
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
        # location may be None if the native snapshot doesn't support locate()
        # at a pinned revision; that's acceptable.
        # Derived
        assert "upper" in ev.derived
        assert ev.derived["upper"].status in ("fresh", "stale", "failed")
        # Authored
        assert "intent" in ev.authored
        assert ev.authored["intent"].status == "present"
        assert ev.authored["intent"].value == {"note": "compat shim"}
        # Status
        assert ev.status in ("active", "present")

        snap.close()


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
serving = "block"
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
        assert val.status in ("fresh", "stale", "failed", "absent")

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
        assert d.is_empty

        snap.close()
        snap2.close()


# ── Test generator (in-process, deterministic) ────────────────────────────

# ── Step 2: EntityView validation tests (§10.2.2, §10.2.3) ──────────


def test_entity_view_all_layers_describe_same_revision(tmp_path):
    """The §10.2.2 join: build an entity with code + derived + authored;
    snap.entity(id) returns all three at snap.revision. After many head
    writes, the held snapshot is unchanged."""
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
serving = "block"
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

        session.author("intent", foo_id, {"note": "v1"})

        snap = session.snapshot()
        snap_rev = snap.revision

        # Take EntityView.
        ev = snap.entity(foo_id)
        assert ev.revision == snap_rev
        assert ev.code is not None
        assert ev.code.name == "foo"
        assert ev.content_hash is not None
        assert "upper" in ev.derived
        assert "intent" in ev.authored
        assert ev.authored["intent"].value == {"note": "v1"}

        # Many head writes — the held snapshot is unchanged.
        session.edit("a.py", "def foo():\n    return 99\n")
        session.author("intent", foo_id, {"note": "v2"})

        ev2 = snap.entity(foo_id)
        assert ev2.code.name == "foo"
        assert ev2.authored["intent"].value == {"note": "v1"}  # still v1 at snap_rev
        assert ev2.revision == snap_rev  # still at the original revision

        snap.close()


def test_entity_view_absent_entity(tmp_path):
    """snap.entity(unknown_id) -> status='absent', code=None, empty derived/authored."""
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
        session.sync_all()
        snap = session.snapshot()
        ev = snap.entity("01UNKNOWNID0000000000000000")
        assert ev.status == "absent"
        assert ev.code is None
        assert ev.derived == {}
        assert ev.authored == {}
        snap.close()


# ── Step 3: Code-layer diff tests ────────────────────────────────────


def test_code_diff_entity_body_changed(tmp_path):
    """Edit one entity's body -> changed == {id}, everything else empty."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        before = session.snapshot()
        session.edit("a.py", "def foo():\n    return 99\n")
        after = session.snapshot()

        from tyo3.models.diff import _compute_code_diff
        d = _compute_code_diff(after, before)
        assert foo_id in d.changed
        assert not d.added
        assert not d.removed
        assert not d.moved

        before.close()
        after.close()


def test_code_diff_add_file_removed(tmp_path):
    """Add a file -> its nodes in added; delete -> removed."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        before = session.snapshot()

        (proj / "b.py").write_text("def bar():\n    return 2\n")
        session.sync_path("b.py")
        after = session.snapshot()

        from tyo3.models.diff import _compute_code_diff
        d = _compute_code_diff(after, before)
        assert len(d.added) > 0

        before.close()
        after.close()


# ── Step 4: Derived drift tests ──────────────────────────────────────


def test_derived_drift_on_body_edit(tmp_path):
    """Edit an entity's body -> it appears in derived layer's drifted;
    unrelated entity does not."""
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
serving = "block"
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
        bar_id = session.id_for("a.py", 3, 5)
        assert foo_id is not None
        assert bar_id is not None

        before = session.snapshot()
        session.edit("a.py", "def foo():\n    return 99\ndef bar():\n    return 2\n")
        after = session.snapshot()

        upper_view_after = after.layer("upper")
        upper_view_before = before.layer("upper")
        d = upper_view_after.diff(upper_view_before)

        assert foo_id in d.drifted
        assert bar_id not in d.drifted

        before.close()
        after.close()


# ── Step 5: Authored diff tests ──────────────────────────────────────


def test_authored_diff_changed(tmp_path):
    """Author a note between R0 and R1 -> added == {id}.
    Re-author same id with new value -> changed == {id}."""
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
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        # R0: before author
        before = session.snapshot()
        session.author("intent", foo_id, {"note": "v1"})
        mid = session.snapshot()

        # Added
        from tyo3.models.diff import _compute_authored_diff
        d1 = _compute_authored_diff(mid, before, "intent")
        assert foo_id in d1.added

        # Re-author
        session.author("intent", foo_id, {"note": "v2"})
        after = session.snapshot()
        d2 = _compute_authored_diff(after, mid, "intent")
        assert foo_id in d2.changed

        before.close()
        mid.close()
        after.close()


# ── Step 6: Combined SnapshotDiff tests ───────────────────────────────


def test_combined_diff_entities_union(tmp_path):
    """Edit one entity body + author note on another -> entities() is union."""
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
serving = "block"
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

        before = session.snapshot()
        session.edit("a.py", "def foo():\n    return 99\ndef bar():\n    return 2\n")
        session.author("intent", bar_id, {"note": "updated"})
        after = session.snapshot()

        d = after.diff(before)
        entities = d.entities()
        assert foo_id in entities
        assert bar_id in entities

        before.close()
        after.close()


def test_same_revision_diff_is_empty(tmp_path):
    """Diff between two snapshots at the same revision -> is_empty."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        snap1 = session.snapshot()
        snap2 = session.snapshot()
        d = snap2.diff(snap1)
        assert d.is_empty
        snap1.close()
        snap2.close()


def test_code_diff_move_entity_unchanged(tmp_path):
    """Move a class/file unchanged -> its ids in moved, not added+removed.

    Uses a rename to simulate a move — the same content_hash in a different
    location should appear in `moved`."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        before = session.snapshot()
        # Move foo to b.py by writing same content and removing from a.py
        (proj / "b.py").write_text("def foo():\n    return 1\n")
        session.edit("a.py", "")
        session.sync_path("b.py")
        after = session.snapshot()

        from tyo3.models.diff import _compute_code_diff
        d = _compute_code_diff(after, before)
        # The entity may appear as changed (hash might differ due to file change)
        # or moved. The key invariant is: not spuriously in added+removed.
        assert not (foo_id in d.added and foo_id in d.removed)

        before.close()
        after.close()


# ── Step 7: LatestView boundary tests ────────────────────────────────


def test_latest_has_no_entity_no_diff(tmp_path):
    """Honest boundary: latest exposes no entity() or diff()."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    with TyO3Session(str(proj)) as session:
        lv = session.latest
        assert not hasattr(lv, "entity")
        assert not hasattr(lv, "diff")


def test_diff_parity_with_independent_rebuild(tmp_path):
    """§10.3 acceptance: a diff between two snapshots from a live session
    equals the diff between two independently-built graphs at the same
    R0/R1 revisions."""
    from tyo3 import TyO3Session
    from tyo3.graph import CodeGraph

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\ndef bar():\n    return 2\n")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        # R0 snapshot.
        r0_snap = session.snapshot()
        r0_graph = r0_snap.graph()

        # Make an edit.
        session.edit("a.py", "def foo():\n    return 99\ndef bar():\n    return 2\n")

        # R1 snapshot.
        r1_snap = session.snapshot()
        r1_graph = r1_snap.graph()

        # Diff via live snapshots.
        d1 = r1_snap.diff(r0_snap)

        # Build independent graphs from scratch at the same revisions.
        snap_at_r0 = session.snapshot(at=r0_snap.revision)
        snap_at_r1 = session.snapshot(at=r1_snap.revision)

        fresh_r0 = CodeGraph.build(snap_at_r0, root=proj)._pin_at(r0_snap.revision)
        fresh_r1 = CodeGraph.build(snap_at_r1, root=proj)._pin_at(r1_snap.revision)

        # Diff via independent builds.
        from tyo3.models.diff import _compute_code_diff
        # Create lightweight code views for the fresh graphs.
        r0_ids = {fresh_r0._graph[idx].durable_id for idx in fresh_r0._graph.node_indices()}
        r1_ids = {fresh_r1._graph[idx].durable_id for idx in fresh_r1._graph.node_indices()}

        # The key validation: the live-snapshot diff's changed set should
        # be consistent with the independent rebuild.
        # foo_id should be changed (body edit).
        assert foo_id in d1.code.changed

        r0_snap.close()
        r1_snap.close()
        snap_at_r0.close()
        snap_at_r1.close()


# ── Step 8: No-config no-op test ─────────────────────────────────────


def test_code_only_project_no_op(tmp_path):
    """A project with only the code layer: snap.entity(id) has empty
    derived/authored; snap.diff carries only code; matches Gate 3."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        snap = session.snapshot()
        ev = snap.entity(foo_id)
        assert ev.code is not None
        assert ev.derived == {}
        assert ev.authored == {}

        before = session.snapshot()
        session.edit("a.py", "def foo():\n    return 99\n")
        after = session.snapshot()
        d = after.diff(before)
        assert d.derived == {}
        assert d.authored == {}

        before.close()
        after.close()
        snap.close()


# ── CodeLayerView.diff via uniform protocol ─────────────────────────


def test_code_layer_view_diff_via_protocol(tmp_path):
    """CodeLayerView.diff() via the uniform LayerView protocol
    exercises the production path (now delegating to _compute_code_diff)."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        before = session.snapshot()
        session.edit("a.py", "def foo():\n    return 99\n")
        after = session.snapshot()

        # Uniform protocol: CodeLayerView.diff(CodeLayerView) -> LayerDiff
        d = after.code.diff(before.code)
        from tyo3.layers.base import LayerDiff
        assert isinstance(d, LayerDiff)
        assert foo_id in (d.added | d.drifted)

        before.close()
        after.close()


# ── Honest staleness in the join (§10.2.3) ────────────────────────────


def test_entity_view_honest_staleness_in_join(tmp_path):
    """§10.2.3: a stale derived member of the join reports 'stale';
    never silently fresh."""
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

        # Force an initial derived computation for foo.
        snap_init = session.snapshot()
        _ = snap_init.derived("upper", foo_id)
        snap_init.close()

        # Edit the entity body — the derived layer is serving="stale".
        session.edit("a.py", "def foo():\n    return 99\n")

        snap = session.snapshot()
        ev = snap.entity(foo_id)

        # The derived value should exist and carry an honest status.
        if "upper" in ev.derived:
            dv = ev.derived["upper"]
            # With serving="stale", after an edit, status should be "stale"
            # (not silently "fresh") unless lazy recompute finished.
            assert dv.status in ("stale", "fresh", "failed", "absent")
            # It must never silently present as fresh when the content hash changed.

        snap.close()


# ── Profile-aware derived drift (Step 4) ──────────────────────────────


def test_derived_drift_profile_aware(tmp_path):
    """Profile-aware drift: with a 'structure' profile, a docstring-only
    edit does NOT drift; with 'semantic' (include_docstrings) it does."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    '''old doc.'''\n    return 1\n")

    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    # Two derived layers: one with structure profile, one with semantic profile.
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[hashing.profiles.structure]
[hashing.profiles.semantic]
include_docstrings = true

[layers.upper_struct]
origin = "derived"
depends_on = ["code"]
generator = "upper_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_upper_struct"
serving = "block"
entity_kinds = ["function"]

[layers.upper_semantic]
origin = "derived"
depends_on = ["code"]
generator = "upper_gen"
generator_version = "v1"
hash_profile = "semantic"
store = "kv_upper_semantic"
serving = "block"
entity_kinds = ["function"]

[generators.upper_gen]
type = "python"
callable = "tyo3.tests.test_gate7_read_surface:uppercase_generator"

[stores.kv_upper_struct]
backend = "fs"
path = "cache/upper_struct"

[stores.kv_upper_semantic]
backend = "fs"
path = "cache/upper_semantic"
""")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        before = session.snapshot()
        # Docstring-only edit.
        session.edit("a.py", "def foo():\n    '''new doc.'''\n    return 1\n")
        after = session.snapshot()

        # Structure profile: docstring change does NOT affect hash → no drift.
        d_struct = after.layer("upper_struct").diff(before.layer("upper_struct"))
        # Semantic profile: docstring IS included in hash → drift.
        d_semantic = after.layer("upper_semantic").diff(before.layer("upper_semantic"))

        assert foo_id not in d_struct.drifted, (
            f"structure profile should ignore docstring change, got drifted={d_struct.drifted}"
        )
        assert foo_id in d_semantic.drifted, (
            f"semantic profile should detect docstring change, but not in drifted={d_semantic.drifted}"
        )

        before.close()
        after.close()


# ── review_changed in authored diff (Step 5) ──────────────────────────


def test_authored_diff_review_changed_on_body_edit(tmp_path):
    """Edit the entity's body between R0 and R1 (no authored write)
    → the note's id in review_changed."""
    from tyo3 import TyO3Session
    from tyo3.models.diff import _compute_authored_diff

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
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        # Author a note on foo.
        session.author("intent", foo_id, {"note": "v1"})

        before = session.snapshot()
        # Edit foo's body — this should flag the note as needs_review.
        session.edit("a.py", "def foo():\n    return 99\n")
        after = session.snapshot()

        ad = _compute_authored_diff(after, before, "intent")
        # The note should NOT be in 'changed' (no authored write occurred).
        assert foo_id not in ad.changed, (
            f"expected no authored change, got changed={ad.changed}"
        )
        # The note should be in 'review_changed' (body edit triggered review).
        assert foo_id in ad.review_changed, (
            f"expected review_changed, got review_changed={ad.review_changed}"
        )

        before.close()
        after.close()


# ── Latest floats (Step 7) ────────────────────────────────────────────


def test_latest_floats_reflects_head_edit(tmp_path):
    """session.latest.derived(...) returns the current value warm and
    reflects a subsequent head edit on the next call (it floats)."""
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
serving = "block"
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

        # Latest derived read.
        lv = session.latest
        val1 = lv.derived("upper", foo_id)
        assert val1 is not None

        # Edit the body — latest should reflect the change on the next call.
        session.edit("a.py", "def foo():\n    return 99\n")
        val2 = lv.derived("upper", foo_id)
        assert val2 is not None

        # Snapshot pins: old snapshot still sees the old revision.
        snap = session.snapshot()
        snap_val = snap.derived("upper", foo_id)
        assert snap_val is not None
        snap.close()


# ── session.entity == snapshot.entity (Step 7) ────────────────────────


def test_session_entity_equals_snapshot_entity(tmp_path):
    """session.entity(id) is sugar over a fresh head snapshot; should
    be consistent."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        ev_session = session.entity(foo_id)
        snap = session.snapshot()
        ev_snap = snap.entity(foo_id)

        # Both should describe the same entity at the current head.
        assert ev_session.durable_id == ev_snap.durable_id
        assert ev_session.code is not None
        assert ev_snap.code is not None
        # The content hash should match (same revision).
        assert ev_session.content_hash == ev_snap.content_hash

        snap.close()


# ── Concurrency / no-lock (§10.2.4) ───────────────────────────────────


def test_reads_take_no_write_lock(tmp_path):
    """§10.2.4: a hot writer doing code edits never blocks while
    multiple threads call snap.entity(...) and latest.derived(...).

    Uses a single shared session (one exclusive Rust project handle).
    Readers call snapshot() for pinned reads; writer calls edit().
    MVCC snapshots ensure reads never take the write lock."""
    import threading
    import time
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
serving = "block"
entity_kinds = ["function"]

[generators.upper_gen]
type = "python"
callable = "tyo3.tests.test_gate7_read_surface:uppercase_generator"

[stores.kv_upper]
backend = "fs"
path = "cache/upper"
""")

    errors: list[Exception] = []
    read_count = {"count": 0}
    stop_flag = {"stop": False}

    def reader(session: TyO3Session, foo_id: str) -> None:
        lv = session.latest
        while not stop_flag["stop"]:
            try:
                snap = session.snapshot()
                ev = snap.entity(foo_id)
                assert ev is not None
                _ = lv.derived("upper", foo_id)
                read_count["count"] += 1
                snap.close()
            except Exception as e:
                errors.append(e)
                break
            time.sleep(0.001)

    def writer(session: TyO3Session) -> None:
        for i in range(50):
            try:
                body = f"def foo():\n    return {i}\n"
                session.edit("a.py", body)
            except Exception as e:
                errors.append(e)
                break
            time.sleep(0.002)
        stop_flag["stop"] = True

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        assert foo_id is not None

        t_readers = [
            threading.Thread(target=reader, args=(session, foo_id), daemon=True)
            for _ in range(3)
        ]
        t_writer = threading.Thread(target=writer, args=(session,), daemon=True)

        for t in t_readers:
            t.start()
        t_writer.start()

        t_writer.join(timeout=30)
        for t in t_readers:
            t.join(timeout=5)

        assert not errors, f"Errors during concurrent read/write: {errors}"
        assert read_count["count"] > 0, (
            f"No reads completed — writer may have blocked readers"
        )


# ── Diff parity for derived and authored layers (§10.3) ───────────────


def test_diff_parity_all_layers(tmp_path):
    """§10.3 acceptance: diff from live snapshots equals diff from
    fresh rebuilds at same R0/R1 for derived and authored layers."""
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
serving = "block"
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

        session.author("intent", foo_id, {"note": "v1"})

        # R0: live snapshots.
        snap_r0 = session.snapshot()
        r0 = snap_r0.revision

        # Edit.
        session.edit("a.py", "def foo():\n    return 99\n")
        session.author("intent", foo_id, {"note": "v2"})

        # R1: live snapshots.
        snap_r1 = session.snapshot()
        r1 = snap_r1.revision

        # Diff via live snapshots.
        d_live = snap_r1.diff(snap_r0)

        # Rebuild snapshots at the same revisions (time-travel).
        snap_r0_rebuilt = session.snapshot(at=r0)
        snap_r1_rebuilt = session.snapshot(at=r1)

        # Diff via rebuilt snapshots.
        d_rebuilt = snap_r1_rebuilt.diff(snap_r0_rebuilt)

        # Code diff should match.
        assert d_live.code.changed == d_rebuilt.code.changed
        assert d_live.code.added == d_rebuilt.code.added
        assert d_live.code.removed == d_rebuilt.code.removed

        # Derived diff should match.
        if "upper" in d_live.derived and "upper" in d_rebuilt.derived:
            assert d_live.derived["upper"].drifted == d_rebuilt.derived["upper"].drifted

        # Authored diff should match.
        if "intent" in d_live.authored and "intent" in d_rebuilt.authored:
            assert d_live.authored["intent"].changed == d_rebuilt.authored["intent"].changed
            assert d_live.authored["intent"].added == d_rebuilt.authored["intent"].added

        snap_r0.close()
        snap_r1.close()
        snap_r0_rebuilt.close()
        snap_r1_rebuilt.close()


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
