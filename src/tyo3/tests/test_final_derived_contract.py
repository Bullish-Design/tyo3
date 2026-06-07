"""Final invariant tests — derived layers (→ Phase 7).

Encodes §5.7 / §5.12: derived invalidation is precise and id-level, staleness
is honest, eager recompute leaks no snapshot, and a generator failure preserves
the last-good artifact.

Today invalidation is fed the path-shaped write result as if it were durable
ids, the per-entity hash lookup raises and is swallowed, so nothing is
invalidated; the eager path also opens a second snapshot it never closes.  The
broken contracts are marked ``xfail(strict=True)`` for Phase 7.
"""

from __future__ import annotations

import gc
import warnings
from pathlib import Path

import pytest

from tyo3 import TyO3Session

# ── Generators referenced by config (callable = "<module>:<name>") ───────────

_CALLS: list[list[str]] = []  # one entry per generate() call: the durable ids seen
_FAIL_NEXT = {"on": False}


def counting_generator(inputs):
    """Deterministic generator: uppercases each input's source; records ids."""
    _CALLS.append([getattr(inp, "durable_id", "?") for inp in inputs])
    return [inp.source.upper().encode() if isinstance(inp.source, str) else inp.source for inp in inputs]


def flaky_generator(inputs):
    """Like ``counting_generator`` but raises when armed via ``_FAIL_NEXT``."""
    if _FAIL_NEXT["on"]:
        raise RuntimeError("intentional generator failure")
    _CALLS.append([getattr(inp, "durable_id", "?") for inp in inputs])
    return [inp.source.upper().encode() if isinstance(inp.source, str) else inp.source for inp in inputs]


def _config(generator: str = "counting_gen", callable_name: str = "counting_generator", serving: str = "stale") -> str:
    return f"""\
schema_version = 1

[hashing.profiles.structure]

[layers.upper]
origin = "derived"
depends_on = ["code"]
generator = "{generator}"
generator_version = "v1"
hash_profile = "structure"
store = "kv_upper"
serving = "{serving}"
entity_kinds = ["function"]

[generators.{generator}]
type = "python"
callable = "tyo3.tests.test_final_derived_contract:{callable_name}"

[stores.kv_upper]
backend = "fs"
path = "cache/upper"
"""


def _open(root: Path, files: dict[str, str], *, config: str) -> TyO3Session:
    (root / "pyproject.toml").write_text("[project]\nname = 'derived'\nversion = '0.1.0'\n")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    cfg = root / ".tyo3"
    cfg.mkdir(exist_ok=True)
    (cfg / "config.toml").write_text(config)
    s = TyO3Session(str(root))
    s.sync_all()
    return s


@pytest.fixture(autouse=True)
def _reset_generator_state():
    _CALLS.clear()
    _FAIL_NEXT["on"] = False
    yield
    _CALLS.clear()
    _FAIL_NEXT["on"] = False


# ── 1. Derived invalidation receives DurableIds ──────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="Phase 7: invalidation is fed the path-shaped write result, so the "
    "generator never sees the changed entity's durable id",
)
def test_invalidation_receives_durable_ids(tmp_path):
    s = _open(tmp_path, {"a.py": "def foo():\n    return 1\n"}, config=_config())
    try:
        foo_id = s.id_for("a.py", 1, 5)
        assert foo_id is not None
        with s.snapshot() as snap:
            assert snap.derived("upper", foo_id).status == "fresh"  # prime cache
        s.edit("a.py", "def foo():\n    return 2\n")  # body change → recompute foo
        with s.snapshot() as snap:
            snap.derived("upper", foo_id)
        seen = {did for call in _CALLS for did in call}
        assert foo_id in seen, "the generator must be invoked with the entity's durable id"
    finally:
        s.close()


# ── 2. A move with unchanged hash reuses the cached artifact ─────────────────


@pytest.mark.xfail(
    strict=True,
    reason="Phase 7: inert invalidation breaks content-hash reuse on move",
)
def test_move_unchanged_reuses_artifact(tmp_path):
    s = _open(tmp_path, {"a.py": "def foo():\n    return 1\n", "c.py": ""}, config=_config())
    try:
        foo_id = s.id_for("a.py", 1, 5)
        with s.snapshot() as snap:
            art0 = snap.derived("upper", foo_id).artifact
        before = len(_CALLS)
        # Move foo to c.py unchanged (same body, same content hash).
        s.edit_many({"a.py": "", "c.py": "def foo():\n    return 1\n"})
        with s.snapshot() as snap:
            val = snap.derived("upper", foo_id)
        assert val.artifact == art0, "moved-unchanged must reuse the artifact"
        assert len(_CALLS) == before, "moved-unchanged must not recompute"
    finally:
        s.close()


# ── 3. A body change recomputes only the affected ids ────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="Phase 7: inert invalidation never schedules the precise recompute",
)
def test_body_change_recomputes_only_affected(tmp_path):
    s = _open(
        tmp_path,
        {"a.py": "def foo():\n    return 1\n", "b.py": "def bar():\n    return 2\n"},
        config=_config(),
    )
    try:
        foo_id = s.id_for("a.py", 1, 5)
        bar_id = s.id_for("b.py", 1, 5)
        with s.snapshot() as snap:
            snap.derived("upper", foo_id)
            snap.derived("upper", bar_id)
        _CALLS.clear()
        s.edit("a.py", "def foo():\n    return 99\n")  # only foo changes
        with s.snapshot() as snap:
            snap.derived("upper", foo_id)
            snap.derived("upper", bar_id)
        recomputed = {did for call in _CALLS for did in call}
        assert foo_id in recomputed, "the changed entity must recompute"
        assert bar_id not in recomputed, "an unaffected entity must not recompute"
    finally:
        s.close()


# ── 4. An eager recompute closes the snapshot it opened (no leak) ─────────────


@pytest.mark.xfail(
    strict=True,
    reason="Phase 7: the eager derivation pass opens a second snapshot it never "
    "closes",
)
def test_eager_recompute_leaks_no_snapshot(tmp_path):
    s = _open(tmp_path, {"a.py": "def foo():\n    return 1\n"}, config=_config(serving="block"))
    try:
        foo_id = s.id_for("a.py", 1, 5)
        with s.snapshot() as snap:
            snap.derived("upper", foo_id)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            s.edit("a.py", "def foo():\n    return 2\n")  # triggers eager recompute
            with s.snapshot() as snap:
                assert snap.derived("upper", foo_id).status == "fresh"
            gc.collect()  # force __del__ of any leaked snapshot → ResourceWarning
    finally:
        s.close()


# ── 5. A generator failure leaves the last-good artifact intact ──────────────


@pytest.mark.xfail(
    strict=True,
    reason="Phase 7: failed staleness/last-good handling depends on id-level "
    "invalidation being live",
)
def test_generator_failure_keeps_last_good(tmp_path):
    s = _open(
        tmp_path,
        {"a.py": "def foo():\n    return 1\n"},
        config=_config(generator="flaky_gen", callable_name="flaky_generator"),
    )
    try:
        foo_id = s.id_for("a.py", 1, 5)
        with s.snapshot() as snap:
            good = snap.derived("upper", foo_id).artifact
        assert good is not None
        # Arm the failure and change the body so a recompute is attempted.
        _FAIL_NEXT["on"] = True
        s.edit("a.py", "def foo():\n    return 2\n")
        with s.snapshot() as snap:
            val = snap.derived("upper", foo_id)
        assert val.status == "failed", f"a generator failure must report failed, got {val.status}"
        assert val.artifact == good, "the last-good artifact must be served intact"
    finally:
        s.close()
