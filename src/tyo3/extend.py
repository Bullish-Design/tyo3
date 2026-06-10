"""``tyo3.extend`` — the programmatic registration API (AB1).

Three process-global registries — ``_LAYERS`` / ``_GENERATORS`` / ``_STORES`` —
plus ``register_layer`` / ``register_generator`` / ``register_store`` and
``load_plugins()``. A developer registers a custom **layer** (authored or
derived), **generator type**, or **store backend** as a real Python object, and
it rides the existing layer-agnostic read/serve/card path with no bespoke wiring
(``API_DESIGN.md`` §0 principle #1).

Built-in generator types (``python``/``command``/``http``) and store backends
(``fs``/``lancedb``) self-register into these registries **at their own import
time** (``derive/generators.py`` / ``stores/__init__.py``), so this module never
imports them at load — there is no import cycle (``AB1_DESIGN_NOTE.md`` §2). The
only runtime-import this module does is ``importlib.metadata`` inside
``load_plugins()``.

Registration is **pre-open**: ``TyO3Session.__init__`` calls ``load_plugins()``
once and re-registers each authored spec into the native validated config before
the layer table freezes (§3). The merge into the engine is the thin
``session.effective_layers`` projection (``AB1_DESIGN_NOTE.md`` §4).
"""

from __future__ import annotations

import importlib
import importlib.metadata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from tyo3.config import GeneratorConfig, LayerConfig
from tyo3.exceptions import TyO3Error

if TYPE_CHECKING:
    from pydantic import BaseModel

    from tyo3.derive.generators import Generator
    from tyo3.session import Snapshot
    from tyo3.sidecar import Sidecar
    from tyo3.stores.base import Store

# ── Shared enums (already in the codebase as strings) ─────────────────────────

KeyLocality = Literal["local", "semantic", "reverse-semantic"]
Serving = Literal["stale", "block"]
Recompute = Literal["lazy", "eager"]
Display = Literal["panel", "inline-note", "inline-summary"]


# ── Spec dataclasses (API_DESIGN §2.2 / §2.3) ─────────────────────────────────


@dataclass(frozen=True)
class AuthoredLayerSpec:
    """A human-entered attachment kind (notes, test links, …).

    Authored layers are commit-validated sinks — they need no generator and no
    store. This is almost entirely *editor + typing* metadata on top of the
    native authored machinery; ``register_authored_layer`` (the one native shim)
    makes the name writable at open.
    """

    name: str
    entity_kinds: tuple[str, ...] = ()
    history: bool = True
    review_on_change: bool = True
    # OPTIONAL typing (AB5). None = free-form JSON (today's behaviour).
    schema: type[BaseModel] | None = None
    # OPTIONAL editor surface (QW5). How the card/panel renders it.
    display: Display = "panel"
    # OPTIONAL: how to turn a value into the inline/decoration string.
    render: Callable[[Any], str] | None = None

    def to_layer_config(self) -> LayerConfig:
        """Project to the thin :class:`LayerConfig` the engine consumes.

        Authored layers normally appear in the native config already (the
        ``register_authored_layer`` shim ran before ``config_json()`` was read),
        so this is only used defensively in the effective-table union.
        """
        return LayerConfig(
            origin="authored",
            depends_on=(),
            generator=None,
            generator_version=None,
            hash_profile=None,
            store=None,
            serving="stale",
            recompute="lazy",
            entity_kinds=tuple(self.entity_kinds),
            history=self.history,
            review_on_change=self.review_on_change,
            key_locality="local",
        )


@dataclass(frozen=True)
class DerivedLayerSpec:
    """A computed attachment kind — a 1:1 superset of :class:`LayerConfig` plus
    ``produce``/``store`` as *objects* (not dotted strings) and optional
    ``schema``/``display``/``render``.

    ``produce`` accepts the legacy ``Generator`` contract
    (``generate(inputs) -> list[bytes]``) **or** a ``"type:<name>"`` /
    ``"mod:fn"`` string. The recording-read-set ``Producer`` protocol is AB2 —
    declared below but not wired this PR.
    """

    name: str
    produce: Producer | Generator | str
    depends_on: tuple[str, ...] = ("code",)
    generator_version: str = "v1"
    hash_profile: str = "structure"
    store: StoreFactory | str = "fs"
    # Cache-key strategy. ``None`` projects to the AB2 **traced** read-set: the
    # framework fingerprints exactly the ids the producer reads through its
    # recording context, so the layer self-heals on forward/reverse/sibling
    # changes by construction. ``local``/``semantic``/``reverse-semantic`` are
    # explicit fast-path overrides that key without running the producer.
    key_locality: KeyLocality | None = None
    serving: Serving = "stale"
    recompute: Recompute = "lazy"
    entity_kinds: tuple[str, ...] = ()
    schema: type[BaseModel] | None = None
    display: Display = "panel"
    render: Callable[[Any], str] | None = None

    def to_layer_config(self) -> LayerConfig:
        """Project to the thin :class:`LayerConfig` the DAG + snapshot consume.

        ``generator``/``store`` are ``None`` here: a registered layer resolves
        its producer/store from this spec's *objects* (or the registries), not
        from ``config.generators``/``config.stores`` (``AB1_DESIGN_NOTE.md`` §4).
        ``key_locality=None`` ⇒ ``traced`` (the AB2 recording-read-set default).
        """
        return LayerConfig(
            origin="derived",
            depends_on=tuple(self.depends_on),
            generator=None,
            generator_version=self.generator_version,
            hash_profile=self.hash_profile,
            store=None,
            serving=self.serving,
            recompute=self.recompute,
            entity_kinds=tuple(self.entity_kinds),
            history=True,
            review_on_change=False,
            key_locality=self.key_locality or "traced",
        )


# ── The Producer protocol (AB2 — declared, not wired this PR) ─────────────────


@runtime_checkable
class ProduceContext(Protocol):
    """A recording read handle to the pinned snapshot (AB2).

    Declared now so ``DerivedLayerSpec.produce`` can be typed against it, but
    **not wired** in AB1 — the recording read-set + cache-key fingerprinting is
    AB2. AB1 producers receive the legacy ``GenInput`` batch.
    """

    durable_id: str
    kind: str
    source: str
    location: str
    snapshot: Snapshot

    def find_references(self) -> list[Any]: ...
    def symbol(self, durable_id: str) -> Any | None: ...
    def dependents(self, durable_id: str | None = None) -> list[str]: ...
    def dependencies(self, durable_id: str | None = None) -> list[str]: ...
    def upstream(self, layer: str) -> Any: ...
    def note_read(self, ids: Iterable[str]) -> None: ...


@runtime_checkable
class Producer(Protocol):
    """Compute artifacts for a batch of entities (AB2 widening).

    Batched like today's ``Generator`` but with a recording snapshot handle.
    Declared now; the recording context is wired in AB2. An object exposing the
    legacy ``generate(inputs) -> list[bytes]`` contract is also accepted as a
    ``DerivedLayerSpec.produce`` value in AB1.
    """

    def produce(self, ctxs: list[ProduceContext]) -> list[Any]: ...

    def setup(self) -> None: ...
    def teardown(self) -> None: ...


# ── Store factory (API_DESIGN §2.5) ───────────────────────────────────────────


@dataclass(frozen=True)
class StoreContext:
    """Everything a store factory needs to open its backing store.

    Built internally by :func:`tyo3.stores.open_store` so the legacy
    ``open_store(store_cfg, sidecar, layer=None)`` signature is preserved
    (``AB1_DESIGN_NOTE.md`` gotcha).
    """

    layer: str | None
    sidecar: Sidecar
    dim: int | None = None
    metric: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


StoreFactory = Callable[["StoreContext"], "Store"]
GeneratorFactory = Callable[..., "Generator"]  # (cfg, *, name="") -> Generator


# ── Process-global registries ─────────────────────────────────────────────────

_LAYERS: dict[str, AuthoredLayerSpec | DerivedLayerSpec] = {}
_GENERATORS: dict[str, GeneratorFactory] = {}  # type-name → factory(cfg, *, name="")
_STORES: dict[str, StoreFactory] = {}  # backend-name → factory(StoreContext)
_PLUGINS_LOADED = False  # module-flag guard for load_plugins()


# ── Registrars ────────────────────────────────────────────────────────────────


def register_layer(spec: AuthoredLayerSpec | DerivedLayerSpec, *, override: bool = False) -> None:
    """Register one attachment kind (authored or derived).

    Idempotent by ``spec.name``: re-registering the same name raises
    ``ValueError`` unless ``override=True``. Must run before
    ``TyO3Session(root)`` opens — the layer table freezes at open (§3).
    """
    name = spec.name
    if name in _LAYERS and not override:
        raise ValueError(f"layer '{name}' is already registered (pass override=True to replace)")
    _LAYERS[name] = spec


def register_generator(name: str, factory: GeneratorFactory, *, override: bool = False) -> None:
    """Register a *generator type* — the seam that replaces editing
    ``make_generator``. ``name`` is what a config ``[generators.g] type=<name>``
    or a ``DerivedLayerSpec.produce="type:<name>"`` resolves to."""
    if name in _GENERATORS and not override:
        raise ValueError(f"generator type '{name}' is already registered (pass override=True to replace)")
    _GENERATORS[name] = factory


def register_store(name: str, factory: StoreFactory, *, override: bool = False) -> None:
    """Register a *store backend* — replaces editing ``open_store``. ``name`` is
    the backend string a layer's store resolves to (e.g. ``qdrant``)."""
    if name in _STORES and not override:
        raise ValueError(f"store backend '{name}' is already registered (pass override=True to replace)")
    _STORES[name] = factory


# ── Discovery via entry points ────────────────────────────────────────────────


def load_plugins() -> None:
    """Import every ``tyo3.plugins`` entry point once (the only new dynamism).

    Each entry point is a zero-arg callable that calls ``register_*()``. Runs
    strictly before ``open()`` so the validated native config and the Python
    layer table merge deterministically. Guarded by ``_PLUGINS_LOADED`` so it is
    a no-op after the first call.
    """
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    _PLUGINS_LOADED = True
    try:
        eps = importlib.metadata.entry_points(group="tyo3.plugins")
    except Exception:
        # A broken metadata cache must never block opening a project.
        return
    for ep in eps:
        try:
            ep.load()()
        except Exception as exc:
            raise PluginLoadError(f"failed to load tyo3 plugin '{ep.name}': {exc}") from exc


class PluginLoadError(TyO3Error):
    """A ``tyo3.plugins`` entry point failed to import or register."""


# ── Resolution helpers (used by DerivationDAG.from_session) ───────────────────


def registered_layer(name: str) -> AuthoredLayerSpec | DerivedLayerSpec | None:
    """The registered spec for *name*, or ``None`` if not registered."""
    return _LAYERS.get(name)


def resolve_generator(produce: Any, *, name: str = "") -> Generator:
    """Build a :class:`Generator` from a ``DerivedLayerSpec.produce`` value.

    - an object exposing ``generate(inputs)`` (legacy contract) → used directly;
    - ``"type:<name>"`` → the registered generator factory for ``<name>``;
    - ``"mod:fn"`` → a built-in ``python`` generator over that dotted callable.
    """
    if hasattr(produce, "generate"):
        return produce  # type: ignore[return-value]
    if isinstance(produce, str):
        if produce.startswith("type:"):
            type_name = produce[len("type:") :]
            factory = _GENERATORS.get(type_name)
            if factory is None:
                raise ValueError(f"layer '{name}': unknown generator type '{type_name}'")
            cfg = GeneratorConfig(
                type=type_name, callable=None, command=(), endpoint=None,
                model=None, dim=None, batch_size=None, concurrency=None, timeout_ms=None,
            )
            return factory(cfg, name=name)
        # "mod:fn" — a python generator over the dotted callable.
        factory = _GENERATORS.get("python")
        if factory is None:
            raise ValueError(f"layer '{name}': 'python' generator type is not registered")
        cfg = GeneratorConfig(
            type="python", callable=produce, command=(), endpoint=None,
            model=None, dim=None, batch_size=None, concurrency=None, timeout_ms=None,
        )
        return factory(cfg, name=name)
    raise ValueError(f"layer '{name}': unsupported produce value {produce!r}")


def resolve_store(store: Any, sidecar: Sidecar, *, layer: str) -> Store:
    """Build a :class:`Store` from a ``DerivedLayerSpec.store`` value.

    - a backend-name string (e.g. ``"fs"``/``"qdrant"``) → dispatched through
      ``open_store`` (the registry);
    - a ``StoreFactory`` callable → invoked with a :class:`StoreContext`.
    """
    if isinstance(store, str):
        from tyo3.stores import open_store

        return open_store({"backend": store}, sidecar, layer=layer)
    if callable(store):
        ctx = StoreContext(layer=layer, sidecar=sidecar, config={})
        return store(ctx)
    raise ValueError(f"layer '{layer}': unsupported store value {store!r}")


__all__ = [
    "AuthoredLayerSpec",
    "DerivedLayerSpec",
    "Display",
    "GeneratorFactory",
    "KeyLocality",
    "PluginLoadError",
    "ProduceContext",
    "Producer",
    "Recompute",
    "Serving",
    "StoreContext",
    "StoreFactory",
    "load_plugins",
    "register_generator",
    "register_layer",
    "register_store",
]
