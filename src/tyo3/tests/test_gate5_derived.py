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


# ── Step 2: DerivedLayer construction and policy ─────────────────────────


class TestDerivedLayer:
    def test_from_config_populates_fields(self, tmp_path):
        from tyo3.config import LayerConfig
        from tyo3.derive.layer import DerivedLayer, Generator
        from tyo3.stores.fs import FsStore

        cfg = LayerConfig(
            origin="derived",
            depends_on=("code",),
            generator="embed_gen",
            generator_version="text-embedding-3@v1",
            hash_profile="semantic",
            store="vectors",
            serving="stale",
            recompute="lazy",
            entity_kinds=("function", "method", "class"),
            history=False,
            review_on_change=False,
        )
        store = FsStore(tmp_path / "cache")

        # Use a dummy generator (real generator comes in Step 3)
        class DummyGen:
            def generate(self, inputs):
                return [b"dummy"] * len(inputs)

        gen = DummyGen()
        layer = DerivedLayer.from_config(cfg, store, gen)
        layer.name = "embeddings"

        assert layer.name == "embeddings"
        assert layer.depends_on == ("code",)
        assert layer.generator_version == "text-embedding-3@v1"
        assert layer.hash_profile == "semantic"
        assert layer.serving == "stale"
        assert layer.recompute == "lazy"
        assert layer.is_code_derived is True
        assert layer.applies_to("function") is True
        assert layer.applies_to("method") is True
        assert layer.applies_to("class") is True
        assert layer.applies_to("variable") is False

    def test_layer_derived_layer(self, tmp_path):
        from tyo3.config import LayerConfig
        from tyo3.derive.layer import DerivedLayer
        from tyo3.stores.fs import FsStore

        cfg = LayerConfig(
            origin="derived",
            depends_on=("descriptions",),
            generator="embed_gen",
            generator_version="v1",
            hash_profile="semantic",
            store="vectors",
            serving="stale",
            recompute="lazy",
            entity_kinds=(),
            history=False,
            review_on_change=False,
        )
        store = FsStore(tmp_path / "cache")

        class DummyGen:
            def generate(self, inputs):
                return [b"dummy"] * len(inputs)

        layer = DerivedLayer.from_config(cfg, store, DummyGen())
        layer.name = "description_embeddings"

        assert layer.is_code_derived is False
        assert layer.depends_on == ("descriptions",)

    def test_entity_kinds_none_means_all(self, tmp_path):
        from tyo3.config import LayerConfig
        from tyo3.derive.layer import DerivedLayer
        from tyo3.stores.fs import FsStore

        cfg = LayerConfig(
            origin="derived",
            depends_on=("code",),
            generator="gen",
            generator_version="v1",
            hash_profile="structure",
            store="kv",
            serving="stale",
            recompute="lazy",
            entity_kinds=(),  # empty → None → all
            history=False,
            review_on_change=False,
        )
        store = FsStore(tmp_path / "cache")

        class DummyGen:
            def generate(self, inputs):
                return [b"dummy"] * len(inputs)

        layer = DerivedLayer.from_config(cfg, store, DummyGen())

        assert layer.entity_kinds is None
        assert layer.applies_to("function") is True
        assert layer.applies_to("variable") is True
        assert layer.applies_to("module") is True


# ── Test generator (call-counting seam) ──────────────────────────────────

_UPPERCASE_CALL_COUNT = 0


# ── Step 3: Generators ──────────────────────────────────────────────────


def _make_python_cfg(callable_ref: str, batch_size: int = 64):
    from tyo3.config import GeneratorConfig
    return GeneratorConfig(
        type="python",
        callable=callable_ref,
        command=(),
        endpoint=None,
        model=None,
        dim=None,
        batch_size=batch_size,
        concurrency=None,
        timeout_ms=None,
    )


def test_python_generator_deterministic():
    """A python generator returns deterministic bytes for a batch."""
    from tyo3.derive.generators import GenInput, PythonGenerator, make_generator

    cfg = _make_python_cfg("tyo3.tests.test_gate5_derived:echo_generator")
    gen = make_generator(cfg, name="test")

    inputs = [
        GenInput(durable_id="a", source="hello"),
        GenInput(durable_id="b", source="world"),
    ]
    results = gen.generate(inputs)
    assert results == [b"hello", b"world"]

    # Deterministic: same inputs → same outputs.
    results2 = gen.generate(inputs)
    assert results2 == results


def test_python_generator_batching():
    """Inputs are split into batches per batch_size."""
    from tyo3.derive.generators import GenInput, make_generator

    cfg = _make_python_cfg("tyo3.tests.test_gate5_derived:echo_generator", batch_size=3)
    gen = make_generator(cfg, name="test")

    inputs = [GenInput(durable_id=str(i), source=f"item{i}") for i in range(7)]
    results = gen.generate(inputs)
    assert len(results) == 7
    assert results == [f"item{i}".encode() for i in range(7)]


def test_python_generator_failure():
    """Generator raising an exception produces GeneratorFailed."""
    from tyo3.derive.generators import GenInput, make_generator
    from tyo3.exceptions import GeneratorFailed

    cfg = _make_python_cfg("tyo3.tests.test_gate5_derived:failing_generator")
    gen = make_generator(cfg, name="test")

    with pytest.raises(GeneratorFailed) as exc_info:
        gen.generate([GenInput(durable_id="x", source="boom")])
    assert exc_info.value.layer == "test"
    assert "x" in exc_info.value.input_ids


def test_command_generator_success():
    """A command generator round-trips stdin→stdout."""
    from tyo3.config import GeneratorConfig
    from tyo3.derive.generators import GenInput, CommandGenerator

    cfg = GeneratorConfig(
        type="command",
        callable=None,
        command=("cat",),
        endpoint=None,
        model=None,
        dim=None,
        batch_size=2,
        concurrency=1,
        timeout_ms=5000,
    )
    gen = CommandGenerator(cfg, name="test")

    inputs = [
        GenInput(durable_id="a", source="hello"),
        GenInput(durable_id="b", source="world"),
    ]
    results = gen.generate(inputs)
    # cat passes stdin through; we expect JSON-encoded lines back
    assert len(results) == 2


def test_command_generator_timeout():
    """A command exceeding timeout raises GeneratorFailed."""
    from tyo3.config import GeneratorConfig
    from tyo3.derive.generators import GenInput, CommandGenerator
    from tyo3.exceptions import GeneratorFailed

    cfg = GeneratorConfig(
        type="command",
        callable=None,
        command=("sleep", "5"),
        endpoint=None,
        model=None,
        dim=None,
        batch_size=1,
        concurrency=1,
        timeout_ms=100,  # 100ms — way too short for sleep 5
    )
    gen = CommandGenerator(cfg, name="test")

    with pytest.raises(GeneratorFailed, match="timed out"):
        gen.generate([GenInput(durable_id="x", source="x")])


def test_command_generator_nonzero_exit():
    """A command with non-zero exit raises GeneratorFailed."""
    from tyo3.config import GeneratorConfig
    from tyo3.derive.generators import GenInput, CommandGenerator
    from tyo3.exceptions import GeneratorFailed

    cfg = GeneratorConfig(
        type="command",
        callable=None,
        command=("false",),
        endpoint=None,
        model=None,
        dim=None,
        batch_size=1,
        concurrency=1,
        timeout_ms=5000,
    )
    gen = CommandGenerator(cfg, name="test")

    with pytest.raises(GeneratorFailed, match="exited"):
        gen.generate([GenInput(durable_id="x", source="x")])


def test_http_generator_stub():
    """HTTP generator with stubbed endpoint returns artifacts."""
    from tyo3.config import GeneratorConfig
    from tyo3.derive.generators import GenInput, HttpGenerator
    from tyo3.exceptions import GeneratorFailed

    cfg = GeneratorConfig(
        type="http",
        callable=None,
        command=(),
        endpoint="http://127.0.0.1:19999/nonexistent",
        model="test-model",
        dim=3,
        batch_size=2,
        concurrency=1,
        timeout_ms=500,
    )
    gen = HttpGenerator(cfg, name="test")

    # This endpoint doesn't exist → should raise GeneratorFailed.
    with pytest.raises(GeneratorFailed, match="HTTP request failed"):
        gen.generate([GenInput(durable_id="x", source="test")])


# ── Generator test helpers ───────────────────────────────────────────────


def echo_generator(inputs):
    """Trivial python generator: returns the source text as-is (encoded)."""
    global _ECHO_CALL_COUNT
    _ECHO_CALL_COUNT += 1
    return [inp.source for inp in inputs]


_ECHO_CALL_COUNT = 0


def failing_generator(inputs):
    """A generator that always raises."""
    raise RuntimeError("intentional failure")


# ── Step 4: Derivation DAG ──────────────────────────────────────────────


class TestDerivationDAG:
    def test_from_session_empty_when_no_layers(self, tmp_path):
        """Empty config → empty DAG."""
        from tyo3 import TyO3Session
        from tyo3.derive.dag import DerivationDAG

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
        (proj / "a.py").write_text("def foo():\n    return 1\n")

        with TyO3Session(str(proj)) as session:
            dag = DerivationDAG.from_session(session)
            assert dag.is_empty

    def test_from_session_builds_code_derived_layer(self, tmp_path):
        """A config with a code-derived layer builds successfully."""
        from tyo3 import TyO3Session
        from tyo3.derive.dag import DerivationDAG

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
generator = "echo_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv"
serving = "stale"

[generators.echo_gen]
type = "python"
callable = "tyo3.tests.test_gate5_derived:echo_generator"

[stores.kv]
backend = "fs"
path = "cache/upper"
""")

        with TyO3Session(str(proj)) as session:
            dag = DerivationDAG.from_session(session)
            assert not dag.is_empty
            upper = dag.layer("upper")
            assert upper.is_code_derived
            assert upper.generator_version == "v1"

    def test_resolve_input_code_derived(self, tmp_path):
        """Resolve input for a code-derived layer returns the profile hash."""
        from tyo3 import TyO3Session
        from tyo3.derive.dag import DerivationDAG

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
generator = "echo_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv"
serving = "stale"

[generators.echo_gen]
type = "python"
callable = "tyo3.tests.test_gate5_derived:echo_generator"

[stores.kv]
backend = "fs"
path = "cache/upper"
""")

        with TyO3Session(str(proj)) as session:
            dag = DerivationDAG.from_session(session)
            snap = session.snapshot()
            foo_id = session.id_for("a.py", 1, 5)
            # If identity is available, test resolve_input.
            if foo_id:
                gen_input, input_hash = dag.resolve_input(
                    dag.layer("upper"), snap, foo_id
                )
                assert gen_input.durable_id == foo_id
                assert len(input_hash) > 0
            snap.close()


def uppercase_generator(inputs):
    """Trivial python generator: returns the uppercased normalised name.

    The 'inputs' list contains GenInput objects with the entity source text.
    We return the uppercased source. This is purely deterministic and needs
    no external models.
    """
    global _UPPERCASE_CALL_COUNT
    _UPPERCASE_CALL_COUNT += len(inputs)
    return [inp.source.upper() for inp in inputs]
