"""Final invariant tests — id-level commit delta (→ Phases 2–3).

Encodes §5.4 / §5.5: every committed write returns one entity-identity-shaped
delta — ``created_ids`` / ``changed_ids`` / ``deleted_ids`` / structured
``moved`` / ``affected_ids`` / ``rescan`` — where ``changed`` means the
content hash changed and a move is reported distinctly from create+delete.

Today the write result is path-shaped (``created/changed/deleted`` are file
paths, ``moved`` are qualified-path strings), so these tests reference fields
that do not exist yet and are marked ``xfail(strict=True)`` for Phase 3.  They
flip to failures (forcing marker removal) once the id-level delta lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tyo3 import TyO3Session


def _open(root: Path, files: dict[str, str], *, config: str | None = None) -> TyO3Session:
    (root / "pyproject.toml").write_text("[project]\nname = 'delta'\nversion = '0.1.0'\n")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    if config is not None:
        cfg = root / ".tyo3"
        cfg.mkdir(exist_ok=True)
        (cfg / "config.toml").write_text(config)
    s = TyO3Session(str(root))
    s.sync_all()
    return s


def _changed_ids(result: Any) -> set[str]:
    return set(result.changed_ids)


def _moved_id(entry: Any) -> str:
    """Pull the durable id out of a structured moved entry (id + locations)."""
    if hasattr(entry, "id"):
        return entry.id
    if isinstance(entry, dict):
        return entry["id"]
    raise AssertionError(f"moved entry is not structured (no id): {entry!r}")


pytestmark = pytest.mark.xfail(
    strict=True,
    reason="Phases 2–3: the id-level CommitDelta (changed_ids/created_ids/"
    "deleted_ids/structured moved/affected_ids) does not exist yet; the write "
    "result is still path-shaped",
)


# ── 1. Editing one function returns its DurableId in changed_ids ─────────────


def test_edit_one_function_reports_its_id(tmp_path):
    s = _open(tmp_path, {"mod.py": "def foo():\n    return 1\n"})
    try:
        foo_id = s.id_for("mod.py", 1, 5)
        assert foo_id is not None
        result = s.edit("mod.py", "def foo():\n    return 2\n")
        assert foo_id in _changed_ids(result), "changed must carry the entity's id"
        assert "mod.py" not in _changed_ids(result), "changed must not carry a file path"
    finally:
        s.close()


# ── 2. Editing two functions in one file reports two distinct ids ────────────


def test_edit_two_functions_reports_two_ids(tmp_path):
    s = _open(tmp_path, {"mod.py": "def foo():\n    return 1\n\ndef bar():\n    return 2\n"})
    try:
        foo_id = s.id_for("mod.py", 1, 5)
        bar_id = s.id_for("mod.py", 4, 5)
        assert foo_id and bar_id and foo_id != bar_id
        result = s.edit("mod.py", "def foo():\n    return 10\n\ndef bar():\n    return 20\n")
        changed = _changed_ids(result)
        assert foo_id in changed and bar_id in changed, "both entity ids must appear"
        assert len({foo_id, bar_id} & changed) == 2
    finally:
        s.close()


# ── 3. A whitespace-only edit returns empty changed_ids ──────────────────────


def test_whitespace_only_edit_changes_nothing(tmp_path):
    s = _open(tmp_path, {"mod.py": "def foo():\n    return 1\n"})
    try:
        result = s.edit("mod.py", "def foo():\n\n    return 1\n")  # blank line inserted
        assert _changed_ids(result) == set(), "cosmetic edit must not appear in changed"
    finally:
        s.close()


# ── 4. A pure move reports one structured moved entry, not create+delete ──────


def test_pure_move_is_structured_and_not_create_delete(tmp_path):
    s = _open(tmp_path, {"a.py": "def helper():\n    return 1\n", "b.py": ""})
    try:
        hid = s.id_for("a.py", 1, 5)
        assert hid is not None
        result = s.edit_many({"a.py": "", "b.py": "def helper():\n    return 1\n"})
        moved_ids = {_moved_id(m) for m in result.moved}
        assert hid in moved_ids, "moved must carry the entity's id"
        assert hid not in set(result.created_ids), "a move is not a create"
        assert hid not in set(result.deleted_ids), "a move is not a delete"
        # The structured entry exposes old and new locations.
        entry = next(m for m in result.moved if _moved_id(m) == hid)
        loc_fields = entry if isinstance(entry, dict) else entry.__dict__
        assert any("old" in str(k) for k in loc_fields), "moved entry needs an old location"
        assert any("new" in str(k) for k in loc_fields), "moved entry needs a new location"
    finally:
        s.close()


# ── 5. Deleting an entity reports its id; authored records become orphaned ────


def test_delete_reports_id_and_orphans_authored(tmp_path):
    config = (
        "schema_version = 1\n\n"
        "[hashing.profiles.structure]\n\n"
        "[layers.intent]\n"
        'origin = "authored"\n'
        "history = true\n"
        "review_on_change = true\n"
    )
    s = _open(tmp_path, {"a.py": "def gone():\n    return 1\n"}, config=config)
    try:
        gid = s.id_for("a.py", 1, 5)
        assert gid is not None
        s.author("intent", gid, {"note": "keep me"})
        result = s.edit("a.py", "")  # delete the entity
        assert gid in set(result.deleted_ids), "deleted must carry the entity's id"
        val = s.authored("intent", gid)
        assert val.status == "orphaned", "authored record must be orphaned, not dropped"
        assert val.value == {"note": "keep me"}, "authored value must survive intact"
    finally:
        s.close()


# ── 6. A coarse change sets rescan = True ────────────────────────────────────


def test_coarse_change_sets_rescan(tmp_path):
    # Editing a project config file is a coarse change: the precise per-entity
    # delta is unknown, so consumers must rebuild everything.
    s = _open(tmp_path, {"a.py": "x = 1\n"})
    try:
        result = s.sync_path("pyproject.toml")
        assert result.rescan is True or result.project_changed is True, (
            "a project-level change must signal rescan/project_changed"
        )
    finally:
        s.close()
