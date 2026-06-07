"""Gate 5 — Derived Layers acceptance tests.

Step 0: Pin the API with a failing self-healing test FIRST.
Step 1: Per-profile content hashes + ArtifactCache.
"""

from __future__ import annotations

import pytest

from tyo3.derive.cache import ArtifactCache, CacheKey
from tyo3.stores.fs import FsStore


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


# ── Step 1: ArtifactCache tests ──────────────────────────────────────────


class TestArtifactCache:
    def test_put_get_round_trip(self, tmp_path):
        store = FsStore(tmp_path / "cache")
        cache = ArtifactCache(store)
        key = CacheKey(input_hash="abc123", generator_version="v1")
        cache.put(key, b"hello")
        assert cache.get(key) == b"hello"
        assert cache.has(key)

    def test_put_same_key_equal_bytes_is_noop(self, tmp_path):
        store = FsStore(tmp_path / "cache")
        cache = ArtifactCache(store)
        key = CacheKey(input_hash="abc123", generator_version="v1")
        cache.put(key, b"hello")
        cache.put(key, b"hello")  # no-op, should not raise
        assert cache.get(key) == b"hello"

    def test_put_same_key_different_bytes_raises(self, tmp_path):
        store = FsStore(tmp_path / "cache")
        cache = ArtifactCache(store)
        key = CacheKey(input_hash="abc123", generator_version="v1")
        cache.put(key, b"hello")
        with pytest.raises(AssertionError, match="conflicting re-put"):
            cache.put(key, b"different")

    def test_key_serialisation_round_trip(self):
        key = CacheKey(input_hash="abc123", generator_version="v1")
        sk = key.to_store_key()
        assert CacheKey.from_store_key(sk) == key


# ── Step 1: Per-profile content hashes ───────────────────────────────────


def test_content_hashes_on_symbols(tmp_path):
    """Symbols from a project with profiles carry per-profile content_hashes."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text(
        "def foo():\n"
        "    \"\"\"A docstring.\"\"\"\n"
        "    return 1\n"
    )

    # Config with two profiles: structure (no docstrings) and semantic (with docstrings)
    cfg_dir = proj / ".tyo3"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("""\
schema_version = 1

[hashing.profiles.structure]
whitespace_insensitive = true
normalize_trailing_commas = true
include_comments = false
include_docstrings = false

[hashing.profiles.semantic]
whitespace_insensitive = true
normalize_trailing_commas = true
include_comments = true
include_docstrings = true
""")

    with TyO3Session(str(proj)) as session:
        symbols = session.document_symbols("a.py")
        foo = next(s for s in symbols if s.name == "foo")

        # Should have per-profile hashes.
        assert "structure" in foo.content_hashes
        assert "semantic" in foo.content_hashes

        # Structure and semantic should differ because docstring is included in semantic.
        assert foo.content_hashes["structure"] != foo.content_hashes["semantic"], (
            f"structure and semantic hashes should differ when docstring changes: "
            f"structure={foo.content_hashes['structure']}, "
            f"semantic={foo.content_hashes['semantic']}"
        )

        # content_hash should match the default profile (structure).
        assert foo.content_hash == foo.content_hashes["structure"]


def test_no_config_noop_content_hashes(tmp_path):
    """With no derived layers configured, content_hashes reflection is empty."""
    from tyo3 import TyO3Session

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    (proj / "a.py").write_text("def foo():\n    return 1\n")

    with TyO3Session(str(proj)) as session:
        symbols = session.document_symbols("a.py")
        foo = next(s for s in symbols if s.name == "foo")

        # With no config override, the default profile "structure" exists.
        # content_hashes should contain at least structure (the default).
        assert "structure" in foo.content_hashes
        # content_hash should match.
        assert foo.content_hash == foo.content_hashes["structure"]


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
