"""Gate 5 — Derived Layers acceptance tests.

Step 0: Pin the API with a failing self-healing test FIRST.
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(reason="Gate 5 Step 0: API does not exist yet — pinned for implementation")
def test_self_healing_derived_layer_cache_hit_recompute_reuse(tmp_path):
    """The three behaviours that define Gate 5.

    Registers a trivial in-process derived layer via config (a python
    generator that returns the uppercased normalised name), over a small
    multi-file project, and asserts:

    1. Cache hit on no-op: edit an *unrelated* entity → artifact reused.
    2. Recompute on change: edit the entity's body → artifact reflects new content.
    3. Reuse on move: move the entity unchanged → artifact reused.
    """
    from tyo3 import TyO3Session

    # Build a small multi-file project.
    proj = tmp_path / "proj"
    proj.mkdir()

    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")

    (proj / "a.py").write_text("def foo():\n    return 1\n")
    (proj / "b.py").write_text("def bar():\n    return 2\n")

    # Write a config with an 'upper' derived layer using a python generator.
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
callable = "tyo3.tests.test_gate5_derived:uppercase_generator"

[stores.kv_upper]
backend = "fs"
path = "cache/upper"
""")

    with TyO3Session(str(proj)) as session:
        # The config should have our 'upper' layer.
        assert "upper" in session.config.layers

        # Get entity ids for foo and bar.
        foo_id = session.id_for("a.py", 1, 5)
        bar_id = session.id_for("b.py", 1, 5)
        assert foo_id is not None
        assert bar_id is not None

        # --- Read at R0: first access populates the cache ---
        snap0 = session.snapshot()
        val0 = snap0.derived("upper", foo_id)
        assert val0.status == "fresh"
        assert val0.artifact is not None
        snap0.close()

        # --- Cache hit on no-op: edit bar (unrelated entity) ---
        call_count_before = _UPPERCASE_CALL_COUNT
        session.edit("b.py", "def bar():\n    return 42\n")
        snap1 = session.snapshot()
        val1 = snap1.derived("upper", foo_id)
        assert val1.status == "fresh"
        # foo's artifact should be reused (same bytes) and NOT recomputed.
        assert val1.artifact == val0.artifact
        assert _UPPERCASE_CALL_COUNT == call_count_before, (
            f"Expected no recompute for foo when bar edited, "
            f"but calls went from {call_count_before} to {_UPPERCASE_CALL_COUNT}"
        )
        snap1.close()

        # --- Recompute on change: edit foo's body ---
        call_count_before = _UPPERCASE_CALL_COUNT
        session.edit("a.py", "def foo():\n    return 99\n")
        snap2 = session.snapshot()
        val2 = snap2.derived("upper", foo_id)
        assert val2.status == "fresh"
        assert val2.artifact != val0.artifact, "foo body changed → artifact should differ"
        assert _UPPERCASE_CALL_COUNT == call_count_before + 1, (
            f"Expected exactly 1 recompute for foo, "
            f"but calls went from {call_count_before} to {_UPPERCASE_CALL_COUNT}"
        )
        snap2.close()

        # --- Reuse on move: move foo to c.py unchanged ---
        call_count_before = _UPPERCASE_CALL_COUNT
        # Simulate move: delete from a.py, create in c.py with same body.
        session.sync_path("a.py")  # remove overlay, revert to disk (old content)
        session.edit("a.py", "")   # clear a.py
        session.edit("c.py", "def foo():\n    return 99\n")  # same body as after edit
        snap3 = session.snapshot()
        val3 = snap3.derived("upper", foo_id)
        assert val3.status == "fresh"
        assert val3.artifact == val2.artifact, "moved unchanged → artifact should be reused"
        assert _UPPERCASE_CALL_COUNT == call_count_before, (
            f"Expected no recompute for moved-unchanged foo, "
            f"but calls went from {call_count_before} to {_UPPERCASE_CALL_COUNT}"
        )
        snap3.close()


# ── Test generator (call-counting seam) ──────────────────────────────────

_UPPERCASE_CALL_COUNT = 0


def uppercase_generator(inputs):
    """Trivial python generator: returns the uppercased normalised name.

    The 'inputs' list contains GenInput objects with the entity source text.
    We return the uppercased source. This is purely deterministic and needs
    no external models.
    """
    global _UPPERCASE_CALL_COUNT
    _UPPERCASE_CALL_COUNT += len(inputs)
    return [inp.source.upper() for inp in inputs]
