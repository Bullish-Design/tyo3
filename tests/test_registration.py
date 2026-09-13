"""AB1 — the programmatic registration API (``tyo3.extend``).

Ports Spikes A/B from ``.scratch/projects/21-.../SPIKE_FINDINGS.md``: a custom
**authored** layer (``tests``) and a custom **derived** layer (``complexity``)
registered *from this test* — zero ``config.toml`` — must author+read, derive,
ride the ``entity_at`` card, decorate inline (QW5), bind to identity like a
built-in (edit ✅ / move ✅ / rename ❌), and be discoverable through the daemon
``layers`` verb. Also covers the registrar dup-raise/override contract, the
entry-point discovery path, and that the built-ins still resolve through the
registries (the refactor is behaviour-identical).
"""

from __future__ import annotations

import ast
import json
import time

import pytest
from pydantic import BaseModel

from tyo3 import TyO3Session
from tyo3.exceptions import SchemaValidationError
from tyo3.extend import (
    AuthoredLayerSpec,
    DerivedLayerSpec,
    register_generator,
    register_layer,
    register_store,
)

try:
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

pytestmark = needs_native


# ── Registry isolation ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registries():
    """Snapshot and restore the process-global registries around every test.

    The registries (``_LAYERS``/``_GENERATORS``/``_STORES``) and the
    ``_PLUGINS_LOADED`` guard are process-global; a leaked registration would
    bleed a custom layer into *other* test files' sessions. Save/restore keeps
    each test hermetic (and keeps the built-in generator/store factories — which
    self-registered at import — intact)."""
    # Force the built-in generator/store factories to self-register *before*
    # snapshotting, so the restore on teardown never wipes them (they register
    # lazily at their modules' import time).
    import tyo3.derive.generators  # noqa: F401
    import tyo3.extend as ext
    import tyo3.stores  # noqa: F401

    layers = dict(ext._LAYERS)
    generators = dict(ext._GENERATORS)
    stores = dict(ext._STORES)
    loaded = ext._PLUGINS_LOADED
    try:
        yield
    finally:
        ext._LAYERS.clear()
        ext._LAYERS.update(layers)
        ext._GENERATORS.clear()
        ext._GENERATORS.update(generators)
        ext._STORES.clear()
        ext._STORES.update(stores)
        ext._PLUGINS_LOADED = loaded


# ── Helpers ───────────────────────────────────────────────────────────────────


class _Complexity:
    """A legacy ``Generator`` (``generate(inputs) -> list[bytes]``) — cyclomatic
    complexity of each entity's own body (Spike B / API_DESIGN §7)."""

    def generate(self, inputs):
        out = []
        for inp in inputs:
            try:
                n = sum(
                    isinstance(x, (ast.If, ast.For, ast.While, ast.And, ast.Or))
                    for x in ast.walk(ast.parse(inp.source))
                )
            except SyntaxError:
                n = 0
            out.append(json.dumps({"score": n + 1}).encode())
        return out


def _make_project(tmp_path, body: str = "def f(x):\n    if x > 0:\n        return x\n    return -x\n"):
    """A minimal **config-less** project — registration is the only source of
    layers, proving the zero-config path."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "p"\n')
    (proj / "m.py").write_text(body)
    return proj


# ── AB2 helpers: a callee/caller project + a real references producer ──────────

_CALLER_LIB = "def target(x: int) -> int:\n    if x > 0:\n        return x + 1\n    return 0\n"
_CALLER_APP = "from lib import target\n\n\ndef caller() -> int:\n    return target(5)\n"


def _make_caller_project(tmp_path):
    """A two-file project where ``app.caller`` calls ``lib.target`` — the Spike C
    setup (a *reverse*-dependency layer lives on the callee)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "p"\n')
    (proj / "lib.py").write_text(_CALLER_LIB)
    (proj / "app.py").write_text(_CALLER_APP)
    return proj


class _RefsProducer:
    """A real *references* producer (now possible — it reads the recording
    snapshot, not just text). Its value is the number of references to the
    entity; ``find_references()`` records the callers into the read-set, so a new
    caller self-heals the cached value. ``calls`` records each produce for the
    recompute-vs-reuse assertions."""

    __test__ = False

    def __init__(self):
        self.calls: list[str] = []

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def produce(self, ctxs):
        out = []
        for c in ctxs:
            self.calls.append(c.durable_id)
            refs = c.find_references()
            out.append(json.dumps({"n": len(refs)}).encode())
        return out


class _CountingComplexity:
    """A legacy ``Generator`` (text-only) that records its invocations — the
    ``{durable_id}`` read-set baseline (keys like ``local``)."""

    __test__ = False

    def __init__(self):
        self.calls: list[str] = []

    def generate(self, inputs):
        self.calls.extend(i.durable_id for i in inputs)
        return [json.dumps({"score": 1}).encode() for _ in inputs]


def _register_tests_and_complexity():
    register_layer(AuthoredLayerSpec(name="tests", entity_kinds=("function", "method"), display="inline-note"))
    register_layer(
        DerivedLayerSpec(
            name="complexity",
            produce=_Complexity(),
            entity_kinds=("function", "method"),
            key_locality="local",
            display="inline-summary",
            render=lambda v: f"cc={v['score']}",
        )
    )


# ── Spike A/B: register → author/derive → ride the card ───────────────────────


def test_registered_layers_author_derive_and_ride_the_card(tmp_path):
    _register_tests_and_complexity()
    proj = _make_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        # The authored layer rode the *native* projection (the shim ran before
        # config_json was read); the derived layer is merged into the effective
        # table only.
        assert "tests" in s.config.layers, "registered authored layer is in the native config projection"
        assert "complexity" not in s.config.layers, "registered derived layer lives only in the effective table"
        assert {"tests", "complexity"} <= set(s.effective_layers)

        did = s.id_for("m.py", 1, 5)
        assert did is not None

        # Author through the existing native path — no bespoke wiring.
        s.author("tests", did, {"paths": ["test_m.py"]})
        av = s.authored("tests", did)
        assert av.value == {"paths": ["test_m.py"]}
        assert av.status == "present"

        # Derive — fresh, reflects the body (f has one `if` ⇒ score 2).
        dv = s.derived("complexity", did)
        assert dv.status == "fresh"
        assert json.loads(dv.artifact) == {"score": 2}

    # Both ride the cross-layer card with zero handler change — exercise the real
    # `entity_at` verb (which calls `_entity_dict` with `s.effective_layers`).
    from tyo3.daemon.handlers import Handlers
    from tyo3.daemon.session_actor import SessionActor

    actor = SessionActor(str(proj))
    actor.start()
    try:
        handlers = Handlers(actor)
        did2 = actor.submit(lambda s: s.id_for("m.py", 1, 5))
        actor.submit(lambda s: s.author("tests", did2, {"paths": ["test_m.py"]}))
        card = handlers.entity_at({"path": "m.py", "line": 1, "col": 5})
        assert card["authored"]["tests"]["value"] == {"paths": ["test_m.py"]}
        assert "complexity" in card["derived"]
        assert json.loads(card["derived"]["complexity"]["artifact"]) == {"score": 2}
    finally:
        actor.stop()


# ── Registrar contract: dup raises, override replaces ─────────────────────────


def test_duplicate_registration_raises_and_override_replaces():
    register_layer(AuthoredLayerSpec(name="tests"))
    with pytest.raises(ValueError, match="already registered"):
        register_layer(AuthoredLayerSpec(name="tests"))
    # override=True replaces.
    register_layer(AuthoredLayerSpec(name="tests", display="panel"), override=True)

    register_generator("mygen", lambda cfg, *, name="": _Complexity())
    with pytest.raises(ValueError, match="already registered"):
        register_generator("mygen", lambda cfg, *, name="": _Complexity())

    register_store("mystore", lambda ctx: None)
    with pytest.raises(ValueError, match="already registered"):
        register_store("mystore", lambda ctx: None)


# ── Entry-point discovery (load_plugins via importlib.metadata) ───────────────


def test_entry_point_plugin_loads(tmp_path, monkeypatch):
    import tyo3.extend as ext

    def _plugin_register():
        register_layer(AuthoredLayerSpec(name="entrypoint_layer", display="inline-note"))

    class _FakeEP:
        name = "entrypoint_layer"

        def load(self):
            return _plugin_register

    def _fake_entry_points(*, group):
        assert group == "tyo3.plugins"
        return [_FakeEP()]

    monkeypatch.setattr(ext.importlib.metadata, "entry_points", _fake_entry_points)
    ext._PLUGINS_LOADED = False  # force a fresh discovery this test

    proj = _make_project(tmp_path)
    # Opening a session runs load_plugins() before open ⇒ the plugin's authored
    # layer rides the native projection.
    with TyO3Session(str(proj)) as s:
        assert "entrypoint_layer" in s.config.layers
        assert s._display_for("entrypoint_layer") == "inline-note"


# ── Built-ins still resolve through the registries ────────────────────────────


def test_builtins_resolve_through_registries():
    import tyo3.extend as ext
    from tyo3.config import GeneratorConfig
    from tyo3.derive.generators import PythonGenerator, make_generator

    assert {"python", "command", "http"} <= set(ext._GENERATORS)
    assert {"fs", "lancedb"} <= set(ext._STORES)

    cfg = GeneratorConfig(
        type="python", callable="os.path:join", command=(), endpoint=None,
        model=None, dim=None, batch_size=None, concurrency=None, timeout_ms=None,
    )
    assert isinstance(make_generator(cfg, name="x"), PythonGenerator)

    with pytest.raises(ValueError, match="Unknown generator type"):
        make_generator(
            GeneratorConfig(
                type="nope", callable=None, command=(), endpoint=None, model=None,
                dim=None, batch_size=None, concurrency=None, timeout_ms=None,
            )
        )


def test_unknown_store_backend_raises_value_error(tmp_path):
    from tyo3.sidecar import Sidecar
    from tyo3.stores import open_store

    sidecar = Sidecar(str(tmp_path))
    with pytest.raises(ValueError, match="Unknown store backend"):
        open_store({"backend": "qdrant"}, sidecar, layer="x")


def test_lancedb_backend_unavailable_raises(tmp_path):
    from tyo3.exceptions import StoreBackendUnavailable
    from tyo3.sidecar import Sidecar
    from tyo3.stores import open_store

    try:
        import lancedb  # noqa: F401
    except ImportError:
        sidecar = Sidecar(str(tmp_path))
        with pytest.raises(StoreBackendUnavailable, match="lancedb"):
            open_store({"backend": "lancedb"}, sidecar, layer="x")


# ── A custom store backend registered + used by a derived layer ───────────────


def test_register_store_backend_used_by_derived_layer(tmp_path):
    from tyo3.stores.fs import FsStore

    opened: list[str] = []

    def _mem_factory(ctx):
        opened.append(ctx.layer)
        # Reuse FsStore under the sidecar cache so the artifact persists.
        return FsStore(ctx.sidecar.cache_dir(ctx.layer))

    register_store("memfs", _mem_factory)
    register_layer(
        DerivedLayerSpec(
            name="complexity",
            produce=_Complexity(),
            entity_kinds=("function",),
            key_locality="local",
            store="memfs",
        )
    )
    proj = _make_project(tmp_path)
    with TyO3Session(str(proj)) as s:
        did = s.id_for("m.py", 1, 5)
        dv = s.derived("complexity", did)
        assert dv.status == "fresh"
    assert opened == ["complexity"], "the registered store backend was used for the layer"


# ── QW5: display-driven inline decoration ─────────────────────────────────────


def test_qw5_registered_layers_decorate_inline(tmp_path):
    _register_tests_and_complexity()
    proj = _make_project(tmp_path)

    from tyo3.daemon.handlers import Handlers
    from tyo3.daemon.session_actor import SessionActor

    actor = SessionActor(str(proj))
    actor.start()
    try:
        handlers = Handlers(actor)
        # The inline pickers select the registered layers by their `display`.
        assert actor.submit(lambda s: handlers._note_layer(s)) == "tests"
        assert actor.submit(lambda s: handlers._summary_layer(s)) == "complexity"

        did = actor.submit(lambda s: s.id_for("m.py", 1, 5))
        actor.submit(lambda s: s.author("tests", did, {"note": "covered"}))

        deco = handlers.decorate({"path": "m.py"})
        entry = next(d for d in deco if d["durable_id"] == did)
        assert entry["note"] == "covered", "the inline-note registered layer decorates"
        assert json.loads(entry["summary"]) == {"score": 2}, "the inline-summary registered layer decorates"
    finally:
        actor.stop()


# ── Daemon `layers` verb lists a registered layer with its display ────────────


def test_daemon_layers_verb_lists_registered_layer(tmp_path):
    _register_tests_and_complexity()
    proj = _make_project(tmp_path)

    from tyo3.daemon.handlers import Handlers
    from tyo3.daemon.session_actor import SessionActor

    actor = SessionActor(str(proj))
    actor.start()
    try:
        handlers = Handlers(actor)
        out = handlers.layers({})["layers"]
        by_name = {layer["name"]: layer for layer in out}
        assert "tests" in by_name and by_name["tests"]["origin"] == "authored"
        assert by_name["tests"]["display"] == "inline-note"
        assert "complexity" in by_name and by_name["complexity"]["origin"] == "derived"
        assert by_name["complexity"]["display"] == "inline-summary"
    finally:
        actor.stop()


# ── AB5: optional per-layer value schemas ─────────────────────────────────────


class TestLinks(BaseModel):
    """The worked example from API_DESIGN §5.1 — a typed ``tests`` value."""

    # Keep pytest from collecting this pydantic model as a test class.
    __test__ = False

    paths: list[str]
    last_run: str | None = None


def test_authored_schema_validates_at_author_time(tmp_path):
    register_layer(AuthoredLayerSpec(name="tests", entity_kinds=("function",), schema=TestLinks))
    proj = _make_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        did = s.id_for("m.py", 1, 5)
        assert did is not None

        # Malformed values are rejected before anything is committed.
        with pytest.raises(SchemaValidationError):
            s.author("tests", did, {"paths": 5})  # wrong type
        with pytest.raises(SchemaValidationError):
            s.author("tests", did, {"wrong": 1})  # missing required field

        # A valid value commits and reads back unchanged (storage stays JSON).
        s.author("tests", did, {"paths": ["test_m.py"]})
        av = s.authored("tests", did)
        assert av.status == "present"
        assert av.value == {"paths": ["test_m.py"]}


def test_unschema_layer_stays_free_form(tmp_path):
    # No schema ⇒ today's behaviour: any JSON-able dict is accepted verbatim.
    register_layer(AuthoredLayerSpec(name="tests", entity_kinds=("function",)))
    proj = _make_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        did = s.id_for("m.py", 1, 5)
        arbitrary = {"anything": [1, 2, 3], "nested": {"k": "v"}}
        s.author("tests", did, arbitrary)
        assert s.authored("tests", did).value == arbitrary


def test_layers_verb_emits_json_schema(tmp_path):
    register_layer(AuthoredLayerSpec(name="tests", entity_kinds=("function",), schema=TestLinks))
    register_layer(AuthoredLayerSpec(name="notes", entity_kinds=("function",)))  # no schema
    proj = _make_project(tmp_path)

    from tyo3.daemon.handlers import Handlers
    from tyo3.daemon.session_actor import SessionActor

    actor = SessionActor(str(proj))
    actor.start()
    try:
        handlers = Handlers(actor)
        by_name = {layer["name"]: layer for layer in handlers.layers({})["layers"]}
        assert by_name["tests"]["schema"] == TestLinks.model_json_schema()
        assert by_name["notes"]["schema"] is None
    finally:
        actor.stop()


# ── Identity binding: edit ✅ / move ✅ / rename ❌ (inherited native path) ──────


def test_registered_authored_record_rides_edit_and_move_not_rename(tmp_path):
    register_layer(AuthoredLayerSpec(name="tests", entity_kinds=("function",), display="inline-note"))
    proj = _make_project(tmp_path, body="def movable(x):\n    return x + 1\n")
    # A known project path for the atomic move, created *before* open so the
    # session recognises it (mirrors test_final_acceptance.py).
    (proj / "moved.py").write_text("")

    with TyO3Session(str(proj)) as s:
        did = s.id_for("m.py", 1, 5)
        assert did is not None
        s.author("tests", did, {"paths": ["t.py"]})

        # ── edit (cosmetic): id + authored record survive ─────────────────
        s.edit("m.py", "def movable(x):\n    # tweak\n    return x + 1\n")
        assert s.id_for("m.py", 1, 5) == did, "id survives a cosmetic edit"
        assert s.authored("tests", did).value == {"paths": ["t.py"]}

        # ── atomic move to a new file: id + authored record survive ────────
        relocated = "def movable(x):\n    # tweak\n    return x + 1\n"
        s.edit_many({"m.py": "", "moved.py": relocated})
        moved_did = s.id_for("moved.py", 1, 5)
        assert moved_did == did, "durable id survives an atomic move"
        assert s.authored("tests", did).value == {"paths": ["t.py"]}, "authored record rides the move"

        # ── rename: identity is NOT preserved (a new id is minted) ─────────
        s.edit("moved.py", "def renamed(x):\n    # tweak\n    return x + 1\n")
        renamed_did = s.id_for("moved.py", 1, 5)
        assert renamed_did is not None
        assert renamed_did != did, "a rename mints a new id (identity hole — AB8)"
        assert s.authored("tests", renamed_did).status == "absent", "the authored record does NOT ride a rename"


# ── AB2: Producer protocol + traced read-sets ─────────────────────────────────


def test_ab2_registered_producer_defaults_to_traced(tmp_path):
    """A registered ``Producer`` with ``key_locality`` omitted keys on the traced
    read-set (the new default), not ``local``."""
    register_layer(DerivedLayerSpec(name="refs", produce=_RefsProducer(), entity_kinds=("function",)))
    proj = _make_caller_project(tmp_path)
    with TyO3Session(str(proj)) as s:
        s.sync_all()
        L = s._get_derivation().layer("refs")
        assert L.key_locality == "traced"
        assert L.is_traced


def test_ab2_traced_references_layer_self_heals_on_new_caller(tmp_path):
    """The Spike C inverse (``spike_c2.py`` port): prime a references layer on the
    callee, add a new caller, **re-read** → the producer recomputes and the value
    reflects the new caller. Today's ``semantic`` references layer is
    stale-forever here; the traced read-set fixes it by construction."""
    prod = _RefsProducer()
    register_layer(DerivedLayerSpec(name="refs", produce=prod, entity_kinds=("function",)))
    proj = _make_caller_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        s.sync_all()
        tid = s.id_for("lib.py", 1, 5)  # the callee `target`
        assert s._get_derivation().layer("refs").is_traced

        # First read primes the cache (cold produce).
        prod.calls.clear()
        v1 = s.derived("refs", tid)
        assert v1.status == "fresh"
        n1 = json.loads(v1.artifact)["n"]
        assert prod.calls == [tid], "cold read produces once"

        # A no-op re-read reuses the cache (cheap self-heal hit, no recompute).
        prod.calls.clear()
        s.derived("refs", tid)
        assert prod.calls == [], "an unchanged re-read does NOT re-run the producer"

        # Add a NEW caller — the callee's own body is untouched, but a *reverse*
        # dependency (the caller's body) changes.
        s.edit("app.py", _CALLER_APP.replace("return target(5)", "return target(5) + target(6)"))
        tid2 = s.id_for("lib.py", 1, 5)
        assert tid2 == tid, "the callee id is stable across the caller edit"

        # Re-read → recomputes (the inverse of stale-forever) and reflects it.
        prod.calls.clear()
        v2 = s.derived("refs", tid2)
        assert tid in prod.calls, "a new caller self-heals the callee's references layer at read"
        n2 = json.loads(v2.artifact)["n"]
        assert n2 > n1, "the recomputed value reflects the new caller"


def test_ab2_legacy_adapter_keys_identically_to_local(tmp_path):
    """A legacy ``Generator`` ridden as a traced producer records read-set
    ``{durable_id}`` ⇒ its cache key is **byte-identical** to the pre-AB2
    ``local`` key (the entity's own content hash)."""
    register_layer(
        DerivedLayerSpec(name="cc_local", produce=_Complexity(), entity_kinds=("function",), key_locality="local")
    )
    register_layer(DerivedLayerSpec(name="cc_traced", produce=_Complexity(), entity_kinds=("function",)))  # → traced
    proj = _make_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        did = s.id_for("m.py", 1, 5)
        dag = s._get_derivation()
        L_local = dag.layer("cc_local")
        L_traced = dag.layer("cc_traced")
        assert not L_local.is_traced and L_traced.is_traced

        v_local = s.derived("cc_local", did)
        v_traced = s.derived("cc_traced", did)
        assert v_local.status == "fresh" and v_traced.status == "fresh"
        assert v_local.artifact == v_traced.artifact, "same generator ⇒ same artifact"

        # The whole point: a {durable_id} read-set degenerates to the `local` key.
        local_key = L_local.binding(did)
        traced_key = L_traced.binding(did)
        assert local_key == traced_key, "the traced {durable_id} key is byte-identical to local"
        with s.snapshot() as snap:
            content_hash = snap.graph().symbol(did).content_hashes["structure"]
        assert local_key == content_hash, "and that key IS the pre-AB2 local key (own content hash)"


def test_ab2_legacy_adapter_no_recompute_on_dependency_change(tmp_path):
    """No regression: a ``{durable_id}`` read-set (legacy adapter) does **not**
    recompute when only a *dependency* changes — it keys exactly like ``local``."""
    prod = _CountingComplexity()
    register_layer(DerivedLayerSpec(name="cc", produce=prod, entity_kinds=("function",)))  # → traced, {durable_id}
    proj = _make_caller_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        s.sync_all()
        cid = s.id_for("app.py", 4, 5)  # the caller
        prod.calls.clear()
        s.derived("cc", cid)
        assert prod.calls == [cid], "cold read produces once"

        # Edit the caller's *dependency* (the callee body). The caller's own
        # content is unchanged ⇒ a {durable_id}-keyed layer must reuse.
        s.edit("lib.py", _CALLER_LIB.replace("return x + 1", "return x + 2"))
        cid2 = s.id_for("app.py", 4, 5)
        assert cid2 == cid
        prod.calls.clear()
        s.derived("cc", cid2)
        assert prod.calls == [], "a dependency-only change does NOT recompute a {durable_id} layer"


def test_ab2_reverse_semantic_override_recomputes_on_caller_change(tmp_path):
    """The ``reverse-semantic`` opt-out override keys on *direct* reverse edges, so
    a changed caller moves the callee's key — recompute at read, no traced
    read-set needed."""
    prod = _CountingComplexity()
    register_layer(
        DerivedLayerSpec(name="rs", produce=prod, entity_kinds=("function",), key_locality="reverse-semantic")
    )
    proj = _make_caller_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        s.sync_all()
        tid = s.id_for("lib.py", 1, 5)
        L = s._get_derivation().layer("rs")
        assert not L.is_traced and L.key_locality == "reverse-semantic"

        prod.calls.clear()
        s.derived("rs", tid)
        assert prod.calls == [tid], "cold read produces once"

        # A changed caller moves the direct-reverse fingerprint ⇒ key miss ⇒ read
        # self-heals (even though commit-time invalidation never saw the callee).
        s.edit("app.py", _CALLER_APP.replace("return target(5)", "return target(5) + target(6)"))
        tid2 = s.id_for("lib.py", 1, 5)
        prod.calls.clear()
        s.derived("rs", tid2)
        assert tid in prod.calls, "reverse-semantic recomputes when a direct caller changes"


def test_ab2_producer_lifecycle_setup_teardown(tmp_path):
    """``Producer.setup()`` runs once at DAG build; ``teardown()`` at session
    close."""
    events: list[str] = []

    class _Lifecycle:
        def setup(self):
            events.append("setup")

        def teardown(self):
            events.append("teardown")

        def produce(self, ctxs):
            return [b"{}" for _ in ctxs]

    register_layer(DerivedLayerSpec(name="life", produce=_Lifecycle(), entity_kinds=("function",)))
    proj = _make_project(tmp_path)
    with TyO3Session(str(proj)) as s:
        did = s.id_for("m.py", 1, 5)
        s.derived("life", did)  # forces the DAG to build (setup)
        assert events == ["setup"], "setup ran once at DAG build"
    assert events == ["setup", "teardown"], "teardown ran at session close"


# ── AB3: Async serve for slow producers ───────────────────────────────────────

_SLEEP = 0.4  # the slow producer's per-call wall-clock cost


class _SlowProducer:
    """A *slow* (LLM/HTTP-style) producer that sleeps in ``produce``. Records each
    produce so we can assert recompute-once (dedup) and off-actor execution."""

    __test__ = False

    def __init__(self, sleep: float = _SLEEP):
        self.sleep = sleep
        self.calls: list[str] = []

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def produce(self, ctxs):
        out = []
        for c in ctxs:
            time.sleep(self.sleep)
            self.calls.append(c.durable_id)
            out.append(json.dumps({"slow": c.durable_id}).encode())
        return out


def _read_until_fresh(s, layer, did, *, timeout=5.0):
    """Poll a derived read until the off-actor worker warms the cache."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dv = s.derived(layer, did)
        if dv.status == "fresh":
            return dv
        time.sleep(0.02)
    return s.derived(layer, did)


def test_ab3_stale_serving_serves_promptly_then_fresh(tmp_path):
    """A ``serving="stale"`` slow producer never blocks the read: the first read
    returns promptly (wall-clock < the producer's sleep) as ``absent``/``stale``,
    and a later read (after the off-actor worker finishes) returns ``fresh`` with
    the produced value."""
    prod = _SlowProducer()
    register_layer(DerivedLayerSpec(name="slow", produce=prod, serving="stale", entity_kinds=("function",)))
    proj = _make_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        did = s.id_for("m.py", 1, 5)
        assert s._get_derivation().layer("slow").is_traced

        # First read returns promptly — far under the producer's sleep — and is
        # honestly non-fresh (no artifact has ever been produced).
        t0 = time.monotonic()
        dv = s.derived("slow", did)
        elapsed = time.monotonic() - t0
        assert elapsed < _SLEEP, f"stale serving must not block on the producer (took {elapsed:.3f}s)"
        assert dv.status == "absent", "no last-good yet ⇒ honest absent, not a blocking produce"

        # The off-actor worker eventually warms the cache ⇒ a later read is fresh.
        fresh = _read_until_fresh(s, "slow", did)
        assert fresh.status == "fresh", "the background recompute warms the cache for the next read"
        assert json.loads(fresh.artifact) == {"slow": did}
        assert prod.calls, "the slow producer ran (off the read thread)"


def test_ab3_block_serving_unchanged(tmp_path):
    """``serving="block"`` keeps today's synchronous behaviour: the first read
    blocks on the producer and returns ``fresh`` immediately (no async path)."""
    prod = _SlowProducer(sleep=0.05)
    register_layer(DerivedLayerSpec(name="blk", produce=prod, serving="block", entity_kinds=("function",)))
    proj = _make_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        did = s.id_for("m.py", 1, 5)
        dv = s.derived("blk", did)
        assert dv.status == "fresh", "a blocking producer's first read produces synchronously ⇒ fresh"
        assert json.loads(dv.artifact) == {"slow": did}
        assert prod.calls == [did], "produced once, inline"


def test_ab3_stale_dedup_single_produce(tmp_path):
    """Two rapid reads of the same missing ``(layer, id, revision)`` enqueue/produce
    **once** — the in-flight dedup keeps a burst of reads from stampeding the
    producer before the first completes."""
    prod = _SlowProducer()
    register_layer(DerivedLayerSpec(name="slow", produce=prod, serving="stale", entity_kinds=("function",)))
    proj = _make_project(tmp_path)

    with TyO3Session(str(proj)) as s:
        did = s.id_for("m.py", 1, 5)
        # Two reads back-to-back, both inside the producer's sleep window ⇒ both
        # enqueue while the first is in flight ⇒ dedup to a single produce.
        d1 = s.derived("slow", did)
        d2 = s.derived("slow", did)
        assert d1.status in ("absent", "stale") and d2.status in ("absent", "stale")

        _read_until_fresh(s, "slow", did)
        assert prod.calls == [did], "the slow producer ran exactly once despite two rapid reads"


def test_ab3_stale_serving_does_not_block_the_actor(tmp_path):
    """Daemon-shaped (off-actor) check: a slow ``serving="stale"`` ``derived`` fired
    on the single actor thread returns promptly and a following fast read is not
    queued behind the slow producer — the produce ran off the actor."""
    from tyo3.daemon.session_actor import SessionActor

    prod = _SlowProducer()
    register_layer(DerivedLayerSpec(name="slow", produce=prod, serving="stale", entity_kinds=("function",)))
    proj = _make_project(tmp_path)

    actor = SessionActor(str(proj))
    actor.start()
    try:
        did = actor.submit(lambda s: s.id_for("m.py", 1, 5))
        t0 = time.monotonic()
        dv = actor.submit(lambda s: s.derived("slow", did))  # the slow layer
        rev = actor.submit(lambda s: s.head)  # a fast read right behind it
        elapsed = time.monotonic() - t0
        assert dv.status == "absent"
        assert isinstance(rev, int)
        # Both actor round-trips together finish well under one producer sleep ⇒
        # the slow produce did NOT run on the actor (it was enqueued off it).
        assert elapsed < _SLEEP, f"the actor must not block on the slow producer (took {elapsed:.3f}s)"
    finally:
        actor.stop()
