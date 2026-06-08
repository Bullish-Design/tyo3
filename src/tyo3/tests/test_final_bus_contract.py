"""Final invariant tests — the subscription bus (→ Phase 6).

Encodes §5.11: every committed write publishes exactly one relevant, id-level
bus delta; deltas to a subscriber arrive in revision order; a slow subscriber
never blocks the writer; and config rejects any writer-blocking overflow
policy.

Today ``discard`` commits a revision but never publishes (a hand-copied
post-commit sequence forgot it), bus ids are path-shaped unless the graph
happens to be materialised, and the ``block`` overflow policy is accepted.  The
failing contracts are marked ``xfail(strict=True)`` for Phase 6.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from tyo3 import TyO3Session
from tyo3.bus.interest import Interest
from tyo3.exceptions import ConfigError

_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _open(root: Path, files: dict[str, str], *, config: str | None = None) -> TyO3Session:
    (root / "pyproject.toml").write_text("[project]\nname = 'bus'\nversion = '0.1.0'\n")
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


def _drain(sub) -> list:
    out = []
    while True:
        d = sub.poll(timeout=0.0)
        if d is None:
            break
        out.append(d)
    return out


_AUTHORED_CFG = (
    "schema_version = 1\n\n"
    "[hashing.profiles.structure]\n\n"
    "[layers.intent]\n"
    'origin = "authored"\n'
    "history = true\n"
    "review_on_change = true\n"
)


def test_every_write_kind_publishes_exactly_one_delta(tmp_path):
    s = _open(
        tmp_path,
        {"a.py": "def foo():\n    return 1\n", "b.py": "def bar():\n    return 2\n"},
        config=_AUTHORED_CFG,
    )
    try:
        fid = s.id_for("a.py", 1, 5)
        sub = s.subscribe(Interest.ALL)
        _drain(sub)  # clear anything from setup

        def count(action) -> int:
            action()
            return len(_drain(sub))

        results = {
            "edit": count(lambda: s.edit("a.py", "def foo():\n    return 11\n")),
            "edit_many": count(
                lambda: s.edit_many(
                    {"a.py": "def foo():\n    return 12\n", "b.py": "def bar():\n    return 22\n"}
                )
            ),
            "edit_virtual": count(lambda: s.edit_virtual("untitled:1", "z = 1\n")),
            "sync_path": count(lambda: s.sync_path("a.py")),
            "discard": count(lambda: s.discard("b.py")),
            "sync_all": count(lambda: s.sync_all()),
            "author": count(lambda: s.author("intent", fid, {"note": "x"})),
        }
        s._inject_changes([("changed", "a.py")])
        results["poll_changes"] = count(lambda: s.poll_changes())

        offenders = {k: v for k, v in results.items() if v != 1}
        assert not offenders, f"each write must publish exactly one delta; got {offenders}"
        sub.close()
    finally:
        s.close()


def test_bus_deltas_are_id_level_and_in_revision_order(tmp_path):
    s = _open(tmp_path, {"a.py": "def foo():\n    return 1\n"})
    try:
        sub = s.subscribe(Interest.ALL)
        _drain(sub)
        s.edit("a.py", "def foo():\n    return 2\n")
        s.edit("a.py", "def foo():\n    return 3\n")
        deltas = _drain(sub)
        assert len(deltas) >= 2
        revs = [d.revision for d in deltas]
        assert revs == sorted(revs), f"deltas must arrive in revision order: {revs}"
        ids = set().union(*(d.changed | d.affected for d in deltas))
        assert ids, "delta must carry entity ids"
        assert all(_ULID.match(i) for i in ids), (
            f"bus delta ids must be durable ids, not file paths: {sorted(ids)[:5]}"
        )
        sub.close()
    finally:
        s.close()


def test_slow_subscriber_does_not_block_writer(tmp_path):
    # A capacity-1 coalescing queue with a subscriber that never consumes: the
    # writer must keep committing without blocking.
    config = (
        "schema_version = 1\n\n"
        "[coordination.bus]\n"
        "queue_capacity = 1\n"
        'overflow = "coalesce"\n'
    )
    s = _open(tmp_path, {"a.py": "x = 0\n"}, config=config)
    try:
        sub = s.subscribe(Interest.ALL)  # never polled — the "slow" subscriber
        start = time.monotonic()
        for i in range(50):
            s.edit("a.py", f"x = {i + 1}\n")
        elapsed = time.monotonic() - start
        assert elapsed < 30.0, f"writer was stalled by a slow subscriber ({elapsed:.1f}s)"
        # The subscriber still has at most its capacity buffered (coalesced).
        assert sub.poll(timeout=0.0) is not None
        sub.close()
    finally:
        s.close()


@pytest.mark.xfail(
    strict=True,
    reason="Phase 6.3: config validation must reject the writer-blocking "
    "'block' overflow policy; today it is silently accepted",
)
def test_config_rejects_writer_blocking_overflow_policy(tmp_path):
    config = (
        "schema_version = 1\n\n"
        "[coordination.bus]\n"
        "queue_capacity = 8\n"
        'overflow = "block"\n'
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'bus'\nversion = '0.1.0'\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    cfg = tmp_path / ".tyo3"
    cfg.mkdir()
    (cfg / "config.toml").write_text(config)
    with pytest.raises(ConfigError):
        TyO3Session(str(tmp_path)).close()
