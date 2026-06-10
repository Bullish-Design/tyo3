"""Durable (level-triggered) needs_review — proj 25, Part A.

`needs_review` / `authored().status` are anchored to the entity's content hash
*at author time* (`AuthoredVersion.reviewed_hash`), compared as a LEVEL against
the current registry anchor hash. This replaces the old edge-triggered behaviour
(status mirrored the per-commit registry `NeedsReview`, which auto-cleared on the
next reconcile — so saving, or editing a neighbouring function, wrongly cleared
the flag, and re-authoring never cleared it).

Matrix (DESIGN §7):
  edit → flagged; save / identical re-commit → still flagged; edit a different
  func in the same file → still flagged; revert body → unflagged; re-author →
  unflagged (acknowledge); cosmetic edit → never; move (same hash) → never;
  review_on_change=false → never; orphaned precedence; v1 record → present.
"""

import json

from tyo3 import TyO3Session

CONFIG = """\
schema_version = 1

[hashing.profiles.structure]

[layers.intent]
origin           = "authored"
history          = true
review_on_change = true

[layers.notes]
origin           = "authored"
history          = true
review_on_change = false
"""


def _make_project(tmp_path, files: dict[str, str]):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
    for name, src in files.items():
        (proj / name).write_text(src)
    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(CONFIG)
    return proj


FOO_V1 = "def foo():\n    return 1\n"
FOO_V2 = "def foo():\n    return 99\n"


def _open_with_note(tmp_path, extra_files=None):
    """A project with foo() carrying an intent note (review_on_change layer)."""
    files = {"a.py": FOO_V1}
    if extra_files:
        files.update(extra_files)
    proj = _make_project(tmp_path, files)
    session = TyO3Session(str(proj))
    session.sync_all()
    foo_id = session.id_for("a.py", 1, 5)
    assert foo_id is not None
    session.author("intent", foo_id, {"note": "load-bearing"})
    assert session.authored("intent", foo_id).status == "present"
    assert session.needs_review() == []
    return proj, session, foo_id


def test_edit_flags_needs_review(tmp_path):
    proj, session, foo_id = _open_with_note(tmp_path)
    with session:
        session.edit("a.py", FOO_V2)
        assert session.authored("intent", foo_id).status == "needs_review"
        assert foo_id in session.needs_review()


def test_identical_recommit_stays_flagged(tmp_path):
    """The headline fix: a save (byte-identical re-commit) must NOT clear it.

    The old engine cleared the flag here — the no-op reconcile re-settled the
    anchor to Active. The level comparison ignores the registry status entirely.
    """
    proj, session, foo_id = _open_with_note(tmp_path)
    with session:
        session.edit("a.py", FOO_V2)
        assert foo_id in session.needs_review()
        # Commit the IDENTICAL body again (the editor's debounce + :w double-commit).
        session.edit("a.py", FOO_V2)
        assert session.authored("intent", foo_id).status == "needs_review"
        assert foo_id in session.needs_review()
        # And once more, for good measure.
        session.edit("a.py", FOO_V2)
        assert foo_id in session.needs_review()


def test_edit_different_func_same_file_stays_flagged(tmp_path):
    """Editing a neighbour in the same file must not clear foo's flag.

    Under the old scoped-reconcile edge semantics this re-settled foo (unchanged
    this commit) back to Active. The level comparison is per-id and stable.
    """
    proj, session, foo_id = _open_with_note(tmp_path, {"a.py": FOO_V1})
    with session:
        # Put foo + bar in one file, re-author against the new layout.
        session.edit("a.py", "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n")
        foo_id = session.id_for("a.py", 1, 5)
        session.author("intent", foo_id, {"note": "load-bearing"})
        assert session.needs_review() == []
        # Flag foo by editing its body.
        session.edit("a.py", "def foo():\n    return 99\n\n\ndef bar():\n    return 2\n")
        assert foo_id in session.needs_review()
        # Now edit ONLY bar — foo is unchanged this commit but must stay flagged.
        session.edit("a.py", "def foo():\n    return 99\n\n\ndef bar():\n    return 222\n")
        assert session.authored("intent", foo_id).status == "needs_review"
        assert foo_id in session.needs_review()


def test_revert_body_unflags(tmp_path):
    """Restoring the reviewed bytes unflags — the key win over a sticky flag."""
    proj, session, foo_id = _open_with_note(tmp_path)
    with session:
        session.edit("a.py", FOO_V2)
        assert foo_id in session.needs_review()
        session.edit("a.py", FOO_V1)  # back to the authored-time body
        assert session.authored("intent", foo_id).status == "present"
        assert session.needs_review() == []


def test_reauthor_acknowledges(tmp_path):
    """Re-authoring re-stamps reviewed_hash = current → the acknowledge action."""
    proj, session, foo_id = _open_with_note(tmp_path)
    with session:
        session.edit("a.py", FOO_V2)
        assert foo_id in session.needs_review()
        # Acknowledge by re-authoring the note against the current body.
        session.author("intent", foo_id, {"note": "load-bearing (reviewed)"})
        assert session.authored("intent", foo_id).status == "present"
        assert session.needs_review() == []
        # A subsequent change re-flags against the NEW baseline.
        session.edit("a.py", "def foo():\n    return 1234\n")
        assert foo_id in session.needs_review()


def test_cosmetic_edit_never_flags(tmp_path):
    """A comment-only change leaves the AST-canonical hash unchanged → present."""
    proj, session, foo_id = _open_with_note(tmp_path)
    with session:
        session.edit("a.py", "def foo():\n    return 1  # a cosmetic comment\n")
        assert session.authored("intent", foo_id).status == "present"
        assert session.needs_review() == []


def test_move_same_hash_never_flags(tmp_path):
    """An atomic move (same body, new path) keeps the hash → never flagged."""
    proj, session, foo_id = _open_with_note(tmp_path, {"b.py": ""})
    with session:
        session.edit_many({"a.py": "", "b.py": FOO_V1})
        # id rides the move; body is byte-identical so the hash is unchanged.
        assert session.id_for("b.py", 1, 5) == foo_id
        assert session.authored("intent", foo_id).status == "present"
        assert session.needs_review() == []


def test_review_on_change_false_never_flags(tmp_path):
    """A notes-layer record (review_on_change=false) is always present."""
    proj = _make_project(tmp_path, {"a.py": FOO_V1})
    with TyO3Session(str(proj)) as session:
        session.sync_all()
        foo_id = session.id_for("a.py", 1, 5)
        session.author("notes", foo_id, {"tag": "fyi"})
        session.edit("a.py", FOO_V2)
        assert session.authored("notes", foo_id).status == "present"
        # The id has no record in any review_on_change layer.
        assert foo_id not in session.needs_review()


def test_orphaned_takes_precedence(tmp_path):
    """Deleting the entity → orphaned (registry-driven), not needs_review."""
    proj, session, foo_id = _open_with_note(tmp_path)
    with session:
        session.edit("a.py", "")  # delete foo
        assert session.authored("intent", foo_id).status == "orphaned"
        assert foo_id not in session.needs_review()
        assert foo_id in session.orphaned()


def test_v1_record_loads_as_present(tmp_path):
    """A legacy v1 record (no reviewed_hash) loads and is never flagged.

    Migration choice (DESIGN §4.5): None ⇒ not flagged; the baseline is set on
    the next author. Author a note (v2), downgrade the sidecar to v1 on disk,
    reopen, change the body — the note must stay present (no baseline to compare).
    """
    proj, session, foo_id = _open_with_note(tmp_path)
    session.close()

    # Downgrade the on-disk record to the v1 shape.
    records = list((proj / ".tyo3" / "authored" / "intent").glob("*.json"))
    assert len(records) == 1, records
    rec_path = records[0]
    doc = json.loads(rec_path.read_text())
    assert doc["format_version"] == 2
    assert "reviewed_hash" in doc["current"]
    doc["format_version"] = 1
    doc["current"].pop("reviewed_hash", None)
    for v in doc.get("history", []):
        v.pop("reviewed_hash", None)
    rec_path.write_text(json.dumps(doc, indent=2) + "\n")

    with TyO3Session(str(proj)) as session2:
        session2.sync_all()
        assert session2.authored("intent", foo_id).value == {"note": "load-bearing"}
        # No baseline → not flagged even after a real body change.
        session2.edit("a.py", FOO_V2)
        assert session2.authored("intent", foo_id).status == "present"
        assert foo_id not in session2.needs_review()
