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
