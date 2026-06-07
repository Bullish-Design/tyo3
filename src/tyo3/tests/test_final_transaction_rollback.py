"""Final invariant tests — transaction atomicity (→ Phase 5).

Encodes §5.3 / §5.10: every write is one native transaction that either fully
publishes revision R or fully rolls back to R−1, with the sidecar as a
participant.  A failure in any in-lock step (identity persist, authored
persist, code-layer update) must leave head, the registry, the authored store,
and the sidecar at the prior revision, and must enqueue no bus delta.

These require a *native test-only fault-injection seam* (§0.4: not filesystem
permission tricks).  That seam does not exist yet, so the tests fail and are
marked ``xfail(strict=True)`` for Phase 5; they flip to failures (forcing
marker removal) once staged commit + rollback land.

Expected seam (Phase 5 to provide): a native method that arms a one-shot fault
at a named commit stage, e.g. ``session._inner._fault_inject("identity_persist")``,
so the next commit raises a typed ``CommitFailed`` / ``SidecarWriteError`` after
that stage and rolls back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tyo3 import TyO3Session
from tyo3.bus.interest import Interest
from tyo3.exceptions import TyO3Error

_FAULT_STAGES = ("identity_persist", "authored_persist", "code_layer")


def _open(root: Path, files: dict[str, str], *, config: str | None = None) -> TyO3Session:
    (root / "pyproject.toml").write_text("[project]\nname = 'rollback'\nversion = '0.1.0'\n")
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


def _arm_fault(session: TyO3Session, stage: str) -> None:
    """Arm a one-shot commit fault at *stage*, or fail if the seam is absent."""
    inner = getattr(session, "_inner", None)
    for attr in ("_fault_inject", "fault_inject", "_inject_commit_fault"):
        fn = getattr(inner, attr, None)
        if fn is not None:
            fn(stage)
            return
    pytest.fail(
        f"Phase 5 must add a native commit fault-injection seam to arm "
        f"stage {stage!r} (e.g. session._inner._fault_inject(stage))"
    )


pytestmark = pytest.mark.xfail(
    strict=True,
    reason="Phase 5: staged commit + rollback and the native fault-injection "
    "seam do not exist yet",
)


def test_identity_persist_failure_rolls_back(tmp_path):
    s = _open(tmp_path, {"a.py": "def foo():\n    return 1\n"})
    try:
        before_head = s.head
        before_id = s.id_for("a.py", 1, 5)
        sub = s.subscribe(Interest.ALL)
        _arm_fault(s, "identity_persist")
        with pytest.raises(TyO3Error):
            s.edit("a.py", "def foo():\n    return 2\n")
        # Head, registry, and bus must all be untouched.
        assert s.head == before_head, "failed commit must not advance head"
        assert s.id_for("a.py", 1, 5) == before_id, "registry must be unchanged"
        assert sub.poll(timeout=0.0) is None, "no bus delta on a rolled-back commit"
        sub.close()
    finally:
        s.close()


def test_authored_persist_failure_rolls_back(tmp_path):
    config = (
        "schema_version = 1\n\n"
        "[hashing.profiles.structure]\n\n"
        "[layers.intent]\n"
        'origin = "authored"\n'
        "history = true\n"
        "review_on_change = true\n"
    )
    s = _open(tmp_path, {"a.py": "def foo():\n    return 1\n"}, config=config)
    try:
        fid = s.id_for("a.py", 1, 5)
        assert fid is not None
        before_head = s.head
        sub = s.subscribe(Interest.ALL)
        _arm_fault(s, "authored_persist")
        with pytest.raises(TyO3Error):
            s.author("intent", fid, {"note": "should not persist"})
        assert s.head == before_head, "failed authored write must not advance head"
        assert s.authored("intent", fid).status == "absent", "authored store unchanged"
        assert sub.poll(timeout=0.0) is None, "no bus delta on a rolled-back commit"
        sub.close()
    finally:
        s.close()


def test_code_layer_failure_publishes_no_partial_revision(tmp_path):
    s = _open(tmp_path, {"a.py": "def foo():\n    return 1\n"})
    try:
        before_head = s.head
        sub = s.subscribe(Interest.ALL)
        _arm_fault(s, "code_layer")
        with pytest.raises(TyO3Error):
            s.edit("a.py", "def foo():\n    return 99\n")
        assert s.head == before_head, "a code-layer failure must publish no revision"
        assert sub.poll(timeout=0.0) is None, "no bus delta on a rolled-back commit"
        sub.close()
    finally:
        s.close()
