"""Final invariant tests — the subscription bus (→ Phase 6 / Phase 7).

Encodes §5.11: every committed write publishes exactly one relevant, id-level
bus delta; deltas to a subscriber arrive in revision order; a slow subscriber
never blocks the writer; and config rejects any writer-blocking overflow
policy.

Phase 6 made the first four contracts hold: every write funnels through one
``_after_commit`` hook so ``discard`` (and every other write) publishes; the
bus delta is an id-level projection of the ``CommitDelta``; the bus asserts
revision order; and config rejects the writer-blocking overflow policy.

Phase 7 makes the bus delta a **pure projection** (no graph, no bridge) and adds
the **refinement-channel** seam (§5.4): a revision-stamped refinement may arrive
after a revision's primary delta, on a channel separate from the primary stream
(so the revision-order assertion is untouched). Nothing emits a refinement yet
(Phase 9) — the contract test injects one manually.
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


def test_refinement_channel_delivers_after_primary_delta(tmp_path):
    """The refinement channel is a separate, ordered stream (§5.4).

    A refinement for an already-delivered revision is delivered on the
    refinement queue, in order, **without** perturbing the primary delta stream
    or its ``revision > last`` assertion (a late refinement for R may legitimately
    arrive after R+1's primary delta). Phase 7 ships the seam only — nothing
    emits a refinement yet — so this injects one manually.
    """
    from tyo3.bus import AffectedRefinement

    s = _open(tmp_path, {"a.py": "def foo():\n    return 1\n"})
    try:
        fid = s.id_for("a.py", 1, 5)
        assert fid is not None
        sub = s.subscribe(Interest.ALL)
        _drain(sub)

        # Two primary commits → two ordered primary deltas.
        r1 = s.edit("a.py", "def foo():\n    return 2\n").revision
        r2 = s.edit("a.py", "def foo():\n    return 3\n").revision
        deltas = _drain(sub)
        revs = [d.revision for d in deltas]
        assert revs == sorted(revs)

        # No refinement has been emitted yet.
        assert sub.poll_refinement(timeout=0.0) is None

        # Inject a refinement for an ALREADY-delivered revision (r1), after r2's
        # primary delta already went out — the late refinement must not trip the
        # primary revision-order assertion.
        bus = s._get_bus()
        bus.publish_refinement(AffectedRefinement(revision=r1, narrowed=frozenset({fid})))

        got = sub.poll_refinement(timeout=1.0)
        assert got is not None
        assert got.revision == r1
        assert fid in got.narrowed

        # The refinement channel is independent — it enqueued no primary delta.
        assert sub.poll(timeout=0.0) is None

        # Refinements are delivered in publish order on their own channel.
        bus.publish_refinement(AffectedRefinement(revision=r2, narrowed=frozenset({fid})))
        bus.publish_refinement(AffectedRefinement(revision=r2 + 1, narrowed=frozenset()))
        order = [sub.poll_refinement(timeout=1.0).revision for _ in range(2)]
        assert order == [r2, r2 + 1]

        # The primary stream is still live and asserts cleanly after refinements.
        s.edit("a.py", "def foo():\n    return 4\n")
        assert _drain(sub), "primary stream must remain live after refinements"
        sub.close()
    finally:
        s.close()


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
