"""Opt-in release-only measurements for the Project 32 evidence table.

Run with ``pytest -m benchmark tests/benchmarks/test_commit_cost.py -s`` after
``devenv shell -- build-release``. The project copy is temporary so disk
synchronisation measurements do not modify the repository or its identity
registry. The identity database is explicitly removed before every run: stale
anchors can resurrect and corrupt id-level delta counts.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FIXTURES_DIR
from tyo3 import TyO3Session, require_release_build
from tyo3.daemon.handlers import Handlers
from tyo3.daemon.session_actor import SessionActor


pytestmark = pytest.mark.benchmark


def _fresh_project(tmp_path: Path) -> Path:
    """Copy a fixture into an isolated project with no identity history."""
    project = tmp_path / "project"
    shutil.copytree(FIXTURES_DIR / "imports", project)
    (project / ".tyo3" / "identity.db").unlink(missing_ok=True)
    return project


def _measure(label: str, operation: Callable[[], Any]) -> tuple[Any, float]:
    """Run and print one evidence-table operation."""
    started = time.perf_counter()
    result = operation()
    elapsed = time.perf_counter() - started
    print(f"{label}: {elapsed:.3f}s")
    return result, elapsed


def test_open_and_commit_costs(tmp_path: Path) -> None:
    """Measure open, edit, disk sync, and five-file batching."""
    require_release_build()
    project = _fresh_project(tmp_path)

    started = time.perf_counter()
    session = TyO3Session(str(project))
    open_elapsed = time.perf_counter() - started
    print(f"open: {open_elapsed:.3f}s")
    try:
        edit_result, _ = _measure(
            "edit",
            lambda: session.edit("overlay.py", "def overlay() -> int:\n    return 1\n"),
        )
        assert len(edit_result.created) == 1

        disk_path = project / "disk.py"
        disk_path.write_text("def disk() -> int:\n    return 1\n")
        sync_result, _ = _measure("sync_path", lambda: session.sync_path("disk.py"))
        assert len(sync_result.created_ids) == 1

        batch_result, _ = _measure(
            "edit_many (5 files)",
            lambda: session.edit_many(
                {
                    f"batch_{index}.py": f"def batch_{index}() -> int:\n    return {index}\n"
                    for index in range(5)
                }
            ),
        )
        assert len(batch_result.created) == 5
    finally:
        session.close()


def test_reindex_check_and_diff_costs(tmp_path: Path) -> None:
    """Measure reindex, cold/cached check, and an id-level revision diff."""
    require_release_build()
    project = _fresh_project(tmp_path)
    session = TyO3Session(str(project))
    before = session.snapshot()
    try:
        reindex_path = project / "reindex.py"
        reindex_path.write_text("def reindexed() -> int:\n    return 1\n")
        reindex_result, _ = _measure("reindex", session.sync_all)
        assert len(reindex_result.created_ids) == 1

        _, cold_elapsed = _measure("check (cold)", session.check)
        _, cached_elapsed = _measure("check (cached)", session.check)
        assert cold_elapsed >= 0
        assert cached_elapsed >= 0

        diff_path = project / "diff.py"
        diff_path.write_text("def diff_target() -> int:\n    return 1\n")
        session.sync_path("diff.py")
        after = session.snapshot()
        try:
            diff, _ = _measure("diff", lambda: after.diff(before))
            assert diff.before_revision < diff.after_revision
        finally:
            after.close()
    finally:
        before.close()
        session.close()


def test_daemon_head_read_cache_cost(tmp_path: Path) -> None:
    """Measure repeated daemon graph reads served by one pinned head snapshot."""
    require_release_build()
    project = _fresh_project(tmp_path)
    actor = SessionActor(str(project))
    actor.start()
    handlers = Handlers(actor)
    try:
        first = handlers.symbols({})
        started = time.perf_counter()
        for _ in range(25):
            result = handlers.symbols({})
        elapsed = time.perf_counter() - started
        print(f"daemon symbols (25 cached reads): {elapsed:.3f}s")
        assert result["revision"] == first["revision"]
    finally:
        actor.submit(lambda _s: handlers.close())
        actor.stop()
