# API_DESIGN — a programmatic Python registration API for TyO3 attachments

> Concrete design for the §2 vision: let a developer **register custom layers /
> generators / stores programmatically** — real Python objects with lifecycles,
> dependencies, and (optional) typed values — so that "attach a new kind of data
> to any AST node" is a low-friction, first-class operation. Grounded in the
> existing types (`config.py`, `derive/generators.py`, `derive/layer.py`,
> `stores/base.py`, `daemon/handlers.py`); see `ASSESSMENT.md` for why each piece
> exists today. Prefer the signatures below over prose.

## 0. Design principles (from the assessment)

1. **The read/serve/card path is already layer-agnostic** (ASSESSMENT §1, §6).
   Registration must feed *that* path, not replace it — a registered layer must
   ride `entity_at`'s card and the nvim panel with zero bespoke wiring.
2. **Rust owns committed truth** (§12). Registration adds **Python-side authored
   and derived layers**, which already live in Python (`DerivationDAG`, authored
   values are JSON). It never teaches Rust new code-layer semantics.
3. **Additive over config, not instead of it.** Built-in layers keep working;
   `config.toml` entries resolve *through* the same registries. Registration is a
   superset of config (§6 migration).
4. **One object per attachment.** Today "a layer" is split across config +
   `make_generator` + `open_store` + a dotted string (§4). The API bundles
   value-shape + production + storage + invalidation + identity behaviour into one
   registered object.
5. **Optional, not mandatory, typing** (ASSESSMENT §9). A layer *may* declare a
   schema; free-form JSON/bytes stays the default.

---

## 1. The registration surface

A single import with module-level registrars and an entry-point hook.

```python
# tyo3.extend  (new module)

from tyo3.extend import (
    register_layer, register_generator, register_store,
    AuthoredLayerSpec, DerivedLayerSpec,
    Producer, ProduceContext, StoreFactory,
    KeyLocality, Serving, Recompute, Display,
)
```

### 1.1 Registrars

```python
def register_layer(spec: AuthoredLayerSpec | DerivedLayerSpec) -> None:
    """Register one attachment kind. Idempotent by spec.name; re-registering the
    same name raises unless override=True. Must be called before TyO3Session(root)
    opens (the layer table is frozen at open — §3)."""

def register_generator(name: str, factory: Callable[[GeneratorConfig], Producer]) -> None:
    """Register a *generator type* (the missing seam — replaces editing
    make_generator). `name` is what a DerivedLayerSpec.produce='type:<name>' or a
    config `[generators.g] type=<name>` resolves to."""

def register_store(name: str, factory: StoreFactory) -> None:
    """Register a *store backend* (replaces editing open_store). `name` is what a
    layer's store backend string resolves to (e.g. 'qdrant', 'sqlite-vec')."""
```

### 1.2 Discovery via entry points

Plugins ship a `tyo3.plugins` entry point; TyO3 imports them at first
`TyO3Session(...)` (before the layer table freezes):

```toml
# a third-party package's pyproject.toml
[project.entry-points."tyo3.plugins"]
tests_layer = "my_pkg.tyo3_ext:register"   # a callable that calls register_*()
```

```python
# my_pkg/tyo3_ext.py
def register() -> None:
    register_layer(AuthoredLayerSpec(name="tests", ...))
```

`importlib.metadata.entry_points(group="tyo3.plugins")` is enumerated once and
each callable invoked; this is the *only* new dynamism and it runs strictly before
`open()`, so the validated native config and the Python layer table are merged
deterministically (§3).

---

## 2. The protocols (concrete dataclasses + Protocols)

These mirror the fields that already exist in `LayerConfig` (`config.py:24-38`)
and `GenInput` (`generators.py:29`) so the migration is mechanical.

### 2.1 Shared enums (already in the codebase as strings)

```python
KeyLocality = Literal["local", "semantic", "reverse-semantic"]  # fast-path OVERRIDES
#   (default when key_locality=None is a TRACED read-set — §2.4 — which keys on
#    exactly the ids the producer touched and is correct for any direction.)
#   local            : key on the entity's own content hash (today)
#   semantic         : + forward dependency-closure fingerprint (today)
#   reverse-semantic : + DIRECT reverse-dep fingerprint (interim, lazy-only, memoized;
#                      see SPIKE_FINDINGS.md Spike C — prefer the traced default)
Serving     = Literal["stale", "block"]          # config.rs ServingMode
Recompute   = Literal["lazy", "eager"]           # config.rs RecomputeMode
Display     = Literal["panel", "inline-note", "inline-summary"]  # NEW (QW5)
EntityKind  = Literal["function","method","class","module", ...] # config.rs is_known_kind
```

### 2.2 Authored layer spec (human-entered attachments)

```python
@dataclass(frozen=True)
class AuthoredLayerSpec:
    name: str
    # which entity kinds this attaches to (empty = all). Mirrors entity_kinds.
    entity_kinds: tuple[EntityKind, ...] = ()
    history: bool = True                 # retain prior versions (authored.rs)
    review_on_change: bool = True        # flag needs_review when entity changes
    # OPTIONAL typing (ASSESSMENT §9 / AB5). None = free-form JSON (today's behaviour).
    schema: type[BaseModel] | None = None
    # OPTIONAL editor surface (QW5). How the card/panel renders it.
    display: Display = "panel"
    # OPTIONAL: how to turn a value into the inline/decoration string.
    render: Callable[[Any], str] | None = None
```

The native commit path already accepts any authored layer name and validates it
(`commit.rs:634`, `methods.rs:307`); an authored layer needs **no generator and no
store** (`config.rs:422-435`). So `AuthoredLayerSpec` is almost entirely *editor +
typing* metadata on top of the existing native authored machinery.

### 2.3 Derived layer spec (computed attachments)

```python
@dataclass(frozen=True)
class DerivedLayerSpec:
    name: str
    depends_on: tuple[str, ...] = ("code",)        # DerivationDAG deps
    produce: Producer | str                        # object, or "type:<name>", or "mod:fn"
    generator_version: str = "v1"                   # cache-key version (rollback)
    hash_profile: str = "structure"
    store: StoreFactory | str = "fs"                # object, or backend name
    # Cache-key strategy. DEFAULT = traced read-set (correct-by-construction, §2.4):
    # the framework keys on exactly the ids the producer touched. `local`/`semantic`/
    # `reverse-semantic` are fast-path OVERRIDES for producers that don't want
    # tracing; see SPIKE_FINDINGS.md Spike C for why a fixed direction enum is not
    # enough on its own (reverse-direction layers are stale-forever under `semantic`).
    key_locality: KeyLocality | None = None        # None = traced read-set (recommended)
    serving: Serving = "stale"
    recompute: Recompute = "lazy"
    entity_kinds: tuple[EntityKind, ...] = ()
    schema: type[BaseModel] | None = None          # optional typed artifact
    display: Display = "panel"
    render: Callable[[Any], str] | None = None
```

This is a 1:1 superset of `LayerConfig` (`config.py:24`) plus `produce`/`store` as
*objects* instead of dotted strings, plus optional `schema`/`display`/`render`.

### 2.4 The Producer protocol (the key widening — AB2) + traced read-sets

Today a generator gets only `GenInput{durable_id, source, kind, meta}`
(`generators.py:29`) — entity text + kind. The new protocol hands it a **read
handle to the pinned snapshot**, so a producer can reach the full `convert/`
surface (references, hover, type-hierarchy) and sibling entities.

**The `ProduceContext` snapshot handle is a *recording* handle.** Every id the
producer touches through it (`find_references`, `symbol`, graph walks, an upstream
`derived(...)`) is logged into a per-call **read-set**, and the framework
fingerprints *exactly that set* into the cache key. This is the resolution to
`SPIKE_FINDINGS.md` Spike C: invalidation then matches the producer's actual data
dependency — forward, reverse, sibling, or mixed — **by construction**, with no
direction enum to get wrong. It is salsa's own dependency-tracking model, which is
apt because ty/salsa *is* the engine underneath. A lazy read self-heals whenever
any traced id changes; `local`/`semantic`/`reverse-semantic` remain available as
fast-path overrides for producers that opt out of tracing.

```python
class ProduceContext(Protocol):
    durable_id: str
    kind: str
    source: str              # entity source text (today's GenInput.source)
    location: str            # file::qualified_path
    # The RECORDING read surface. Every call appends the resolved ids to this
    # context's read-set, which the framework fingerprints into the cache key.
    def find_references(self) -> list[Reference]: ...
    def symbol(self, durable_id: str) -> Node | None: ...
    def dependents(self, durable_id: str | None = None) -> list[str]: ...   # reverse edges
    def dependencies(self, durable_id: str | None = None) -> list[str]: ... # forward edges
    def upstream(self, layer: str) -> Any: ...        # a depends_on layer's value
    # escape hatch: the raw frozen snapshot (reads here are NOT auto-traced;
    # the producer may call ctx.note_read(ids) to record them explicitly).
    snapshot: "Snapshot"
    def note_read(self, ids: Iterable[str]) -> None: ...

class Producer(Protocol):
    """Compute artifacts for a batch of entities. Batched like today's Generator,
    but with a recording snapshot handle. Returns one artifact per input
    (bytes | typed model | None)."""
    def produce(self, ctxs: list[ProduceContext]) -> list[bytes | BaseModel | None]: ...

    # OPTIONAL lifecycle (the thing dotted-string callables can't have):
    def setup(self) -> None: ...        # called once at DAG build (open a client, …)
    def teardown(self) -> None: ...     # called at session close
```

The existing `python`/`command`/`http` generators (`generators.py`) become
built-in `Producer` adapters registered under those names — so config stays valid
and the legacy `generate(inputs)->list[bytes]` contract is wrapped (§6). Those
adapters read only entity text, so their traced read-set is `{durable_id}` ⇒ they
key exactly like `local` today (no behaviour change).

### 2.5 Store factory (replaces the open_store dispatch)

```python
StoreFactory = Callable[["StoreContext"], Store]   # Store is the existing protocol

@dataclass(frozen=True)
class StoreContext:
    layer: str
    sidecar: "Sidecar"        # gives cache_dir(layer), root
    dim: int | None
    metric: str | None
    config: dict[str, Any]    # backend-specific extras
```

`Store`/`VectorStore` are **unchanged** (`stores/base.py:9-27`):
`get/put/has/delete` (+ `nearest` for vectors). `register_store("qdrant", factory)`
makes a backend first-class instead of the current `not implemented` stub
(`stores/__init__.py:39-46`).

---

## 3. How registration reaches the engine (the wiring)

The only non-obvious integration question: the layer set is **fixed at `open()`
and validated in Rust** (`config.rs::validate`, projected read-only into
`TyConfig`, `config.py:112`). Decision: **registration runs before open and
produces a Python-side layer table that is *merged with* the validated native
config.**

```
register_*()  ──►  _REGISTRY (process-global)
                        │
TyO3Session(root):
  1. import tyo3.plugins entry points (once)            # §1.2
  2. native open() + validate native config.toml        # unchanged; Rust truth
  3. build the effective layer table =
       native config.layers  ⊕  _REGISTRY layers        # NEW merge step
       (name collision: registered overrides only with override=True)
  4. DerivationDAG.from_session reads the MERGED table   # dag.py:39 today reads config
       - produce/store resolved through _REGISTRY (objects) or built-in adapters
  5. authored layers: merged table feeds _entity_dict / author validation
```

Crucially:
- **Rust still owns code truth and validates what's in `config.toml`.** Registered
  layers are *additional* authored/derived layers that live entirely on the Python
  side — exactly where `DerivationDAG` and authored JSON already live (§12 safe).
- The native `author` for a registered authored layer: the native validator
  (`commit.rs:634` `authored_layer_config`) currently only knows config layers. So
  authored layers must **either** be declared in `config.toml` **or** the native
  `author` validation must accept a Python-registered authored layer. Cleanest:
  `register_layer` for an authored layer **writes a synthesized config entry into
  the in-memory validated config at open** (a single native call
  `register_authored_layer(name, history, review_on_change)`), so the native
  commit path keeps validating — one small native addition, no new code semantics.
- Derived layers need **no** native change: `DerivationDAG` is pure Python and
  invalidation is driven by id-level deltas (`session.py:_invalidate_derived`).

This keeps the blast radius tiny: one native shim for authored-layer registration;
everything else is the existing Python assembly reading a merged table.

---

## 4. How it reaches the daemon + the editor

Registration is useless if the editor can't see it. Three additions (the QW items
in `ROADMAP.md`), all generic:

1. **`layers` RPC** (QW3): returns the merged table as
   `[{name, origin, entity_kinds, history, review_on_change, display, has_schema}]`.
   The plugin builds its author menu from this — no more hardcoded `intent`/`docs`
   (`notes.lua:12`, `actions.lua:37`).
2. **`entity_at` already serves every layer** (`_entity_dict`, `handlers.py:324,333`)
   — a registered layer rides the card with **zero** handler change. The renderer
   already iterates all layers (`card.lua:115,122`). This is the payoff of
   principle #1: *surfacing is already generic.*
3. **`display`/`render`** (QW5): the handler's `_note_layer`/`_summary_layer`
   hardcode (`handlers.py:347-359`) is replaced by "layers whose `display` is
   `inline-note`/`inline-summary`," so a registered layer can opt into inline
   extmarks declaratively. (Pair with ROADMAP QW6: today the panel/compact
   renderers gate on `status == "present"`, which derived records never satisfy
   — fix that or a registered derived layer shows in the float but not the panel.)
4. **Pushability for free** (QW7/AB7): the bus already supports
   `Interest.layer(name)` (`interest.py:44`), so a registered layer's *updates* can
   be streamed (`subscribe(Interest.layer("X"))`) once `touched_layers` is
   populated on the published delta — no new mechanism, just wiring.

Net editor experience: **author a registered layer → it appears in the card, the
panel, and (if `display` says so) inline — without writing Lua.**

---

## 5. Worked examples

### 5.1 A `tests` layer (authored) — link entities to their tests

```python
from tyo3.extend import register_layer, AuthoredLayerSpec
from pydantic import BaseModel

class TestLinks(BaseModel):
    paths: list[str]          # test files/ids covering this entity
    last_run: str | None = None

register_layer(AuthoredLayerSpec(
    name="tests",
    entity_kinds=("function", "method", "class"),
    schema=TestLinks,                       # author-time validation (optional)
    display="inline-note",
    render=lambda v: f"✓ {len(v['paths'])} tests",
))
```
That is the **whole** integration. `author("tests", id, {"paths": [...]})` works
through the existing native path; the card shows it; the panel lists it; the inline
extmark renders `✓ N tests`. Rename still drops it (inherits the identity hole,
ASSESSMENT §2) — until ROADMAP AB8 (an explicit native rename rebind) lands; note
that routing rename through an atomic move does **not** rescue it (Spike D), so
this needs the small native change, not a daemon trick.

### 5.2 `references` / callers — a **live RPC**, deliberately *not* a layer

> ⚠ **The instinct is to make "who calls this" a derived layer. Don't** —
> `SPIKE_FINDINGS.md` Spike C proved a references layer is *stale-forever* under
> `semantic` locality, and even a reverse fix is wasted effort here: the engine
> recomputes references in milliseconds (Spike E) and caching cheap reverse data
> buys nothing while taking on the hardest invalidation in the system. **Cheap +
> reverse ⇒ serve it live, not cached.**

```python
# No registration. Just a daemon RPC that wraps the surface Python already exposes:
def references(self, params):              # add to handlers._METHODS (QW1)
    path, line, col = params["path"], params["line"], params["col"]
    def work(s):
        return [r.model_dump(mode="json")
                for r in s.find_references(self._relpath(path), line, col)]  # _ReadOps
    return self._actor.submit(work)
```
This is the ROADMAP QW1 move: expose the stranded `convert/` surface (ASSESSMENT
§7) as live verbs. The editor gets callers/neighbors/diagnostics/hover/rename with
no cache to invalidate. **Reserve the layer machinery for data that is genuinely
worth caching** (§5.2b).

### 5.2b An expensive-reverse layer — where caching *is* worth it (traced read-set)

When the reverse-direction value is *expensive* — e.g. an LLM that summarises an
entity by reading all of its call-sites — caching is justified, and the **traced
read-set** keys it correctly with no locality reasoning:

```python
from tyo3.extend import register_layer, DerivedLayerSpec, ProduceContext

class CallsiteSummary:
    def setup(self):  self.llm = LLMClient()
    def produce(self, ctxs: list[ProduceContext]):
        out = []
        for c in ctxs:
            refs = c.find_references()          # RECORDED into c's read-set automatically
            sites = [c.symbol_at(r) for r in refs]   # also recorded
            out.append(self.llm.summarise(c.source, sites).encode())
        return out

register_layer(DerivedLayerSpec(
    name="callsite_summary",
    produce=CallsiteSummary(),
    # key_locality omitted ⇒ TRACED read-set: the framework fingerprints exactly the
    # callee + every call-site id the producer touched. Add a caller ⇒ the read-set
    # changes ⇒ a lazy read self-heals. Correct by construction (§2.4) — no enum.
    store="fs", serving="stale", recompute="lazy", display="panel",
))
```
This is impossible today (the `Generator` only gets text, §4.3). The
recording `ProduceContext` makes "expensive value derived from my dependents" an
ordinary cached, correctly-invalidated layer — the one quadrant of Spike C's matrix
that genuinely needs the layer machinery, solved without a fragile direction enum.

### 5.3 `diagnostics` — also a live RPC (cheap), not a layer

By the same cost×direction rule (§5.2), diagnostics are cheap and the engine
already computes them on demand (`check`/`check_file` are *on the RPC table
today*). So the recommendation is **not** a `diagnostics` derived layer — it is to
read `check_file` live and filter to the entity's range editor-side (or add a
`diagnostics_at(path,line,col)` verb). Caching diagnostics would mean invalidating
on every edit to the file *and its imports* for no latency benefit. Layer machinery
is the wrong tool; QW1's live surface is the right one.

### 5.4 A custom embedding layer (derived + vector store + lifecycle)

```python
from tyo3.extend import register_layer, register_store, DerivedLayerSpec

class STEmbedder:                         # stateful — loads a model once
    def setup(self):  self.model = SentenceTransformer("all-MiniLM-L6-v2")
    def teardown(self): self.model = None
    def produce(self, ctxs):
        vecs = self.model.encode([c.source for c in ctxs])
        return [v.astype("float32").tobytes() for v in vecs]

register_store("qdrant", lambda sc: QdrantStore(sc.config["url"], dim=sc.dim, metric=sc.metric))
register_layer(DerivedLayerSpec(
    name="embed", produce=STEmbedder(), generator_version="minilm@v1",
    store="qdrant", key_locality="semantic", hash_profile="semantic",
    entity_kinds=("function","method","class","module"),
))
```
`STEmbedder.setup/teardown` is the lifecycle a dotted-string `module:function`
cannot have (§4.3); `register_store("qdrant", ...)` is the backend the current
`open_store` stub can't reach (`stores/__init__.py:39-46`). `session.nearest(...)`
(`views.py:262`) works against it unchanged — it already dispatches to any store
with a `nearest` method.

---

## 6. Migration from the config-string model

**Zero breaking changes; config becomes sugar over the registry.**

| Today (`config.toml`) | After |
|---|---|
| `[layers.X] origin="derived" generator="g" store="s"` | still valid — resolved through the registry at open (§3) |
| `[generators.g] type="python" callable="mod:fn"` | `python` is a **built-in registered generator type**; the dotted callable is wrapped in a `Producer` adapter |
| `[generators.g] type="http"/"command"` | built-in registered types (existing `HttpGenerator`/`CommandGenerator` become adapters) |
| `[stores.s] backend="fs"/"lancedb"` | built-in registered store factories |
| editing `make_generator` for a new type | `register_generator("mytype", factory)` |
| editing `open_store` for a new backend | `register_store("mybackend", factory)` |
| hardcoding `intent`/`docs` in Lua | the `layers` RPC (QW3) + `display` metadata |

Implementation order (matches ROADMAP AB1):
1. Introduce `_REGISTRY` and seed it with the current built-in generator types and
   store backends (refactor `make_generator`/`open_store` to *consume* the registry
   — behaviour-identical, covered by existing tests).
2. Add `register_*` + entry-point discovery + the merged-table step in
   `DerivationDAG.from_session` / session open.
3. Add the one native `register_authored_layer` shim (§3).
4. Add `layers` RPC + `display`/`schema` plumbing to the daemon and `card.lua`.
5. Add the contract test (ASSESSMENT §10): a custom authored + custom derived
   layer registered from a test, asserted to ride the card, invalidate per
   `key_locality`, and bind to identity like a built-in.

---

## 7. "Define a new attachment in N lines" — the end-to-end narrative

Goal: a `complexity` attachment — a derived integer per function, shown inline,
recomputed only when the function body changes.

```python
# myproject/tyo3_ext.py   (referenced by a tyo3.plugins entry point)
from tyo3.extend import register_layer, DerivedLayerSpec
import ast, json

class Complexity:
    def produce(self, ctxs):
        out = []
        for c in ctxs:
            n = sum(isinstance(x, (ast.If, ast.For, ast.While, ast.And, ast.Or))
                    for x in ast.walk(ast.parse(c.source)))
            out.append(json.dumps({"score": n + 1}).encode())
        return out

def register():
    register_layer(DerivedLayerSpec(
        name="complexity",
        produce=Complexity(),
        entity_kinds=("function", "method"),
        key_locality="local",          # only my own body matters → no over-recompute
        store="fs",
        display="inline-summary",
        render=lambda v: f"cc={v['score']}",
    ))
```

```toml
# myproject/pyproject.toml
[project.entry-points."tyo3.plugins"]
complexity = "myproject.tyo3_ext:register"
```

That is the entire contribution — **~20 lines, no engine edit, no daemon edit, no
Lua.** On the next `TyO3Session`/daemon start:
- the entry point registers the layer (§1.2);
- `DerivationDAG` picks it up from the merged table (§3) — it caches, and
  invalidates only when the function body's content hash changes
  (`key_locality="local"`, `dag.py:237`);
- `entity_at` includes it in the card automatically (`_entity_dict`,
  `handlers.py:333`);
- the panel lists it and, because `display="inline-summary"`, an extmark shows
  `cc=N` next to each function;
- identity rides edits and atomic moves (and, once AB8's explicit native rename
  rebind lands, renames) like every other layer.

This is the §2 vision realized: *what to attach to any AST node* is a small,
self-contained Python object a user registers — not a core edit, not a dotted
string, not a config-only indirection.

---

## 8. Open questions

1. **Authored-layer native validation (§3).** Synthesize a config entry at open
   via a native `register_authored_layer` shim, or relax `commit.rs:634` to accept
   a Python-supplied authored-layer allow-list? The shim is cleaner (keeps Rust the
   validator); confirm it fits the commit-plan flow.
2. ~~**Invalidation soundness for snapshot-reading producers.**~~ **RESOLVED by
   `SPIKE_FINDINGS.md` Spike C — and the answer is a taxonomy, not one enum value.**
   A reverse-direction layer is **stale-forever** under both `semantic` and
   affected-driven invalidation (0 recompute after a new caller, verified). The
   resolution is by cost×direction: (a) **cheap reverse** (references, diagnostics) ⇒
   serve as a **live RPC, not a layer** (QW1) — caching cheap reverse data is all
   cost, no benefit; (b) **expensive reverse** ⇒ key on a **traced read-set**
   (§2.4): the recording `ProduceContext` fingerprints exactly the ids the producer
   touched, so any direction is correct by construction (salsa's model). (c)
   `reverse-semantic` (direct, lazy, memoized) remains only as an interim override.
   Folded into §2.1/§2.4, the §5.2/§5.2b/§5.3 examples, and ROADMAP AB2.
3. **Schema transport to the editor.** JSON Schema (language-neutral, the plugin
   can introspect) vs. a name only (`has_schema`)? Lean JSON Schema via
   `model_json_schema()` so a future non-Lua client benefits.
4. **Registration timing.** Strictly pre-open is simplest and matches the frozen
   layer table. Is a runtime `reload`-triggered re-registration ever needed (e.g.
   a plugin hot-reload)? Defer unless the workflow demands it.
5. **Producer error model.** Reuse `GeneratorFailed` (`generators.py`) with its
   honest `failed`/`stale` serving (`views.py:249-260`) — confirm the typed-model
   return path funnels through the same failure accounting.
