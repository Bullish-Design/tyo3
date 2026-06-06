# Gate 5 — Derived Layers, Content-Hash Cache & the Derivation DAG: Implementation Guide

> Implements **REFINED_SPEC.md §8** (derived-layer cache and invalidation) and
> **§9** (the derivation DAG orchestrator) in full, plus the derived half of
> **§10.2.2** (a snapshot resolves derived artifacts by content hash at its
> revision). It is the first *knowledge* layer above the code layer — the thing
> that makes embeddings, docstrings, and descriptions self-healing.
>
> **Prerequisites:** `gate4-complete`. This gate consumes Gate 4's `ValidatedConfig`
> (the topologically-sorted layer set), the `Store`/`VectorStore` protocols and
> `cache/<layer>/` scaffolding, `ValidatedConfig::hash_policy_for`, and the unified
> error taxonomy. It consumes Gate 3's code graph (`durable_id → content_hash` at a
> revision) and the `SyncResult` delta, and Gate 2's content hashing.
>
> **Audience:** an engineer new to the codebase. Same rules as Gates 1–4:
> `devenv shell -- <script>` for everything; one labelled commit per validated step
> (`gate5: step N — …`); never skip a validation.

## How to work in this repo

- Rust unit tests: `devenv shell -- test-rust`. Full suite: `devenv shell -- tests`.
- Rebuild after Rust changes: `devenv shell -- build`.
- Property tests (for the precision/parity fuzzing in Step 5/9): `devenv shell -- test-property`.
- Branch off `gate4-complete`; commit per validated step.

## The contract you are building toward (read first)

A **derived layer** is a pure function of `(input @ R, generator_version)`, cached
by content so it never recomputes unnecessarily and never serves a wrong answer:

- **Content-addressed, immutable cache (§8.2.1/§8.2.2).** Artifacts are keyed by
  `(input_hash, generator_version)`, *not* by revision or `DurableId`. A given key
  maps to exactly one immutable artifact, so the cache is trivially consistent
  across revisions and agents and needs no revision awareness.
- **Precise invalidation, by hash comparison (§8.2.3, §8.3).** On a delta, for each
  affected entity the *new* input hash is compared to the cached binding:
  **unchanged ⇒ reuse (no recompute)**, **changed/missing ⇒ stale ⇒ schedule
  recompute**. An entity that merely *moved* (body unchanged) keeps its hash and so
  hits the cache for free (§5.5.1). Unrelated entities are never touched
  (**no over-fire**) and changed entities are never missed (**no miss**).
- **A DAG that terminates (§9).** Derived layers declare dependencies forming a DAG
  rooted at code; recompute walks it in topological order; authored layers are
  **sinks**, never sources, so reactions always terminate. Scheduling is
  **idempotent** and a failure **leaves the prior artifact intact**.
- **Honest staleness at a snapshot (§8.2.5, §10.2.3).** A snapshot resolves a
  derived value by the entity's content hash at R; a stale value is served as
  last-good **tagged `stale`** (default) or blocks until recompute (per policy),
  never silently presented as fresh.

The defining success property: **edit one entity → only its (and genuinely
hash-affected) artifacts recompute; move it unchanged → its artifact is reused;
bump `generator_version` → a fresh key space, prior keys retained; a project with
no derived layers configured behaves exactly as Gate 4.**

## Design stance

This gate is **almost entirely Python**, and that is the point. The spine (Rust)
already owns everything a derived layer needs — content hashes on symbols, the
reconciled delta, the pinned read surface — so the layer runtime plugs into existing
seams rather than reaching into the core. The one small Rust change (Step 1:
per-profile hashes) is the clean way to let layers key on their own normalisation
policy, and it is gated so that an un-configured project pays nothing. Keep the
spine/layer separation crisp: **Rust = spine; Python = layers and orchestration**,
exactly as the code graph (Gate 3) already is. If no derived layers are declared,
the DAG is empty and every hook in this gate is a no-op.

## The central simplification (read before designing anything)

Content-addressing makes invalidation *local*. A derived artifact for entity `X`
depends only on `X`'s own normalised content (its hash). So when `X` changes, only
`X`'s artifact is stale — **its importers are not**, because their hashes did not
change. The reverse-dependency closure (Gate 3 §4.3.3) is what the *subscription
bus* (Gate 8) and *code-layer edge revalidation* (Gate 3) need; a per-entity derived
layer does **not** use it. Invalidation here is just: *for each entity in
`created ∪ changed`, compare its new hash to the cached one.* This is the whole
"no over-fire" guarantee, and it is why the cache is cheap. Do not reintroduce the
closure into derived invalidation — that would over-fire by design.

For a **layered** derivation (`description_embeddings` depends on `descriptions`),
the input is not code but the upstream artifact, and the input hash is the **hash of
that upstream artifact**. So the cascade is driven by upstream *artifact* hashes
changing, walked in topological order — still local, still content-addressed.

## Target module layout

```
src/tyo3/
  derive/                  # NEW: the derived-layer runtime
    __init__.py
    cache.py               # ArtifactCache: (input_hash, generator_version) keyed, put-once (Step 1)
    layer.py               # DerivedLayer: one layer's config + store + generator (Step 2)
    generators.py          # python/command/http dispatch + GeneratorFailed (Step 3)
    dag.py                 # DerivationDAG: registration, topo walk, invalidate (Steps 4–5)
    scheduler.py           # idempotent, topo-ordered recompute worker (Step 6)
  stores/
    lancedb_store.py       # NEW: lazy VectorStore adapter (Step 8)
  models/
    derived.py             # NEW: DerivedValue { artifact, status, revision } (Step 7)
  session.py               # own the DAG; wire invalidation into the write path (Step 5)
  exceptions.py            # add GeneratorFailed (Step 3)
rust/src/
  entity.rs / dto          # per-profile content hashes on the symbol DTO (Step 1)
src/tyo3/tests/
  test_gate5_derived.py
```

---

## Step 0 — Pin the API with a failing self-healing test FIRST

**Goal.** Before building anything, write the headline acceptance test against the
*intended* public API, so the API shape is decided up front and the machinery has a
fixed target (mirrors Gate 3 Step 0). It will fail (or `xfail`) until Step 7.

**Files.** `src/tyo3/tests/test_gate5_derived.py` (new).

**Build the fixture & test.** Register a trivial **in-process** derived layer via
config (a `python` generator that returns, say, the uppercased normalised name — no
models, fully deterministic), over a small multi-file project. Assert the three
behaviours that define the gate:
- **Cache hit on no-op:** read `snap.derived("upper", id)` at R0; edit an *unrelated*
  entity; at R1 the artifact for `id` is the *same object/bytes* and was **not**
  recomputed (assert via a generator call counter).
- **Recompute on change:** edit `id`'s body; at R2 its artifact reflects the new
  content and the counter incremented exactly once.
- **Reuse on move:** move `id` to another file unchanged; at R3 the artifact is
  reused (counter unchanged) — the §5.5.1 payoff at the derived layer.

**Validate.**
- `devenv shell -- tests -k gate5_derived` runs; record status (expected: failing/
  xfail — the API does not exist yet).
- **Acceptance gate:** the test exists, names the intended API
  (`session.config` layer `upper`, `snap.derived(layer, id)`, a generator
  call-counter seam), and its status is recorded. Commit
  (`gate5: step 0 — pin derived-layer API, status=XFAIL`).

> Do not implement anything in Step 0. Its job is to freeze the API and the success
> criteria so Steps 1–7 build toward a fixed contract.

---

## Step 1 — Per-profile content hashes + the cache-key model

**Goal.** Let each layer key its cache on a hash computed under *its* normalisation
profile (§8.2.1), and formalise the immutable `(input_hash, generator_version)` key.
Gate 4 deferred per-layer hashes to here; this is that seam.

**Files.** `rust/src/entity.rs` + the symbol DTO (Rust); `src/tyo3/derive/cache.py`
(Python); the graph node payload (`src/tyo3/graph/models.py`).

**Build.**
- **Rust — per-profile hashes (gated).** In `extract_entities`, compute the entity
  hash under **each distinct hash profile referenced by a configured layer** (always
  including `default_hash_profile`), using `ValidatedConfig::hash_policy_for`. Expose
  on the symbol DTO a small map `content_hashes: {profile_name: hash_hex}` alongside
  the existing default `content_hash` (which stays the identity/reconciliation hash,
  unchanged). **With no derived layers, the only profile is the default**, so this is
  exactly today's single hash at zero extra cost — the no-config no-op.
- The graph node (`SymbolNode`) carries `content_hashes` through from the DTO (still
  a small payload — a handful of 128-bit hashes, no vectors/text, §6.2.2).
- **Python — `ArtifactCache`** (a thin, enforcing wrapper over a Gate-4 `Store`):
```python
@dataclass(frozen=True)
class CacheKey:
    input_hash: str          # hex of the entity (or upstream artifact) hash under the layer's profile
    generator_version: str

class ArtifactCache:
    """Content-addressed, immutable per key (§8.2.2). Put-once: re-putting the same
    key with different bytes is a programming error (raises)."""
    def __init__(self, store: Store): ...
    def get(self, key: CacheKey) -> bytes | None: ...
    def put(self, key: CacheKey, artifact: bytes) -> None: ...   # idempotent for equal bytes; raises on conflicting re-put
    def has(self, key: CacheKey) -> bool: ...
```
  Key serialisation: `f"{input_hash}:{generator_version}"`. The cache has **no
  revision awareness** — that is the whole point of §8.2.2.

**Validate.**
- Rust test: with a config declaring a `semantic` profile (`include_docstrings=true`)
  and a `structure` layer, an entity's `content_hashes` has *different* hashes under
  the two profiles when only a docstring differs, and *equal* hashes when it does not.
- Rust test: with **no** derived layers, `content_hashes == {default_profile: <the
  existing identity hash>}` — byte-identical to Gate 4.
- Python test: `ArtifactCache.put` then `get` round-trips; `put` of the same key with
  equal bytes is a no-op; `put` of the same key with different bytes raises
  (immutability, §8.2.2).
- **Acceptance gate:** all pass. Commit.

---

## Step 2 — The `DerivedLayer` runtime object

**Goal.** One object that owns a single derived layer's behaviour, reading its
contract entirely from `session.config` (Gate 4) — no per-layer code.

**Files.** `src/tyo3/derive/layer.py`.

**Build.**
```python
class DerivedLayer:
    """Runtime for one declared derived layer. Pure policy + cache + generator;
    no scheduling (Step 6) and no DAG wiring (Step 4) yet."""
    name: str
    depends_on: tuple[str, ...]          # e.g. ("code",) or ("descriptions",)
    generator: Generator                  # Step 3
    generator_version: str
    hash_profile: str
    serving: Literal["stale", "block"]
    recompute: Literal["lazy", "eager"]
    entity_kinds: frozenset[str] | None   # None = all
    cache: ArtifactCache

    def keys_for(self, input_hash: str) -> CacheKey: ...
    def is_fresh(self, input_hash: str) -> bool:      # cache.has(key)
    def applies_to(self, kind: str) -> bool: ...      # entity_kinds filter
```
- `DerivedLayer.from_config(layer_cfg, store, generator)` constructs it from the
  Gate-4 `LayerConfig`. The store comes from `open_store(layer_cfg.store, sidecar)`.
- A layer is **code-derived** (`depends_on == ("code",)`) or **layer-derived**
  (`depends_on` names another derived layer). This distinction decides where the
  *input* and *input_hash* come from (Step 4). Encode it as a property
  `is_code_derived`.

**Validate.**
- Test: construct from the full annotated config's `embeddings` layer →
  fields populated; `applies_to("function")` true, `applies_to("variable")` false
  (per `entity_kinds`).
- Test: a layer-derived layer (`description_embeddings`) reports
  `is_code_derived == False` and `depends_on == ("descriptions",)`.
- **Acceptance gate:** all pass. Commit.

---

## Step 3 — Generators (dispatch + failure semantics)

**Goal.** Produce an artifact for an input, across the three generator types, with
batching/concurrency/timeout and the §9.2.6 failure rule.

**Files.** `src/tyo3/derive/generators.py`, `src/tyo3/exceptions.py`.

**Build.**
- A `Generator` protocol: `generate(inputs: list[GenInput]) -> list[bytes]` (batched).
  `GenInput` carries the resolved input (entity source text for code-derived layers,
  or the upstream artifact bytes for layer-derived) plus minimal metadata.
- Three implementations dispatched by `generator_cfg.type`:
  - **`python`** — import `module:callable`, call in-process (pure, fast path).
  - **`command`** — spawn argv; entity payload on stdin, artifact on stdout; honour
    `timeout_ms`, `concurrency` (a bounded pool), `batch_size`.
  - **`http`** — POST to `endpoint` with `model`/`dim`; honour `timeout_ms`,
    `concurrency`, `batch_size`; credentials resolved from env (Gate 4 `${VAR}` rule),
    never from config.
- **Failure (§9.2.6):** any generator error/timeout raises a typed
  `GeneratorFailed` (add to the Gate-4 taxonomy under `TyO3Error`), carrying the
  layer, the offending input ids, and the cause. The caller (scheduler, Step 6)
  guarantees the **prior artifact is untouched** and the target is marked
  `failed` — the generator itself simply raises; it never writes a partial artifact.

**Validate.**
- Test: a `python` generator returns deterministic bytes for a batch; batching splits
  a >`batch_size` input set into the right number of calls.
- Test: a `command` generator round-trips stdin→stdout; a non-zero exit or a timeout
  raises `GeneratorFailed` naming the layer + ids.
- Test: `http` generator with a stubbed endpoint returns artifacts; a 500/timeout
  raises `GeneratorFailed`.
- Test: a generator referencing `${OPENAI_KEY}` reads it from the env; absent →
  `ConfigError` at config load (Gate 4), not here.
- **Acceptance gate:** all pass. Commit.

---

## Step 4 — The Derivation DAG: registration & input resolution

**Goal.** Assemble the derived layers into a DAG from the *already-validated* config
(Gate 4), and define how each layer resolves its input + input hash for an entity
(§9.2.1).

**Files.** `src/tyo3/derive/dag.py`.

**Build.**
```python
class DerivationDAG:
    """Owns all DerivedLayers, in the topological order Gate 4 already computed.
    Authored layers are NOT here — they are sinks (§9.2.4)."""
    def __init__(self, layers: list[DerivedLayer], topo_order: list[str]): ...
    @classmethod
    def from_session(cls, session) -> "DerivationDAG": ...   # reads session.config
```
- **Consume Gate 4's topo order; do not recompute or re-validate acyclicity** — it
  is guaranteed by config validation (§9.2.2 static). Assert it defensively once
  (cheap) and move on. Authored layers are excluded as *sources* (§9.2.4); a config
  that named one as a `depends_on` was already rejected in Gate 4.
- **Input resolution** — the one piece of real logic here:
```python
def resolve_input(self, layer, snapshot, durable_id) -> tuple[GenInput, str]:
    if layer.is_code_derived:
        node = snapshot.graph().node(durable_id)
        input_hash = node.content_hashes[layer.hash_profile]   # Step 1
        return GenInput(source=snapshot.entity_source(durable_id), ...), input_hash
    else:                                  # layer-derived (e.g. description_embeddings)
        upstream = self.layer(layer.depends_on[0])
        up_value, up_hash = upstream.read(snapshot, durable_id)   # the upstream artifact + its hash
        return GenInput(source=up_value), sha(up_hash + ...)       # input_hash = hash of upstream artifact
```
  This is the central rule: **a code-derived layer keys on the entity's profile
  hash; a layer-derived layer keys on the upstream artifact's hash.** Both are
  content-addressed, so both cascades are local and self-healing.
- An empty config → empty DAG; `from_session` returns a DAG that no-ops everywhere.

**Validate.**
- Test: `from_session` over the full annotated config builds layers in topo order;
  `code`-rooted; `description_embeddings` ordered after `descriptions`.
- Test: `resolve_input` for a code-derived layer returns the profile hash from the
  pinned graph node; for a layer-derived layer returns a hash derived from the
  upstream artifact (mock the upstream).
- Test: empty config → empty DAG, all operations no-op.
- **Acceptance gate:** all pass. Commit.

---

## Step 5 — Invalidation from the delta (the in-lock §3 step-5 seam)

**Goal.** On every committed delta, mark exactly the hash-affected artifacts stale —
no miss, no over-fire (§8.2.3, §8.3) — **inside the same write-path serialization
Gate 3 established** for the head-graph update (§3.3.1).

**Files.** `src/tyo3/session.py`, `src/tyo3/derive/dag.py`.

**Build.**
- The session owns a lazily-built `DerivationDAG` alongside `_head_graph`.
- Extend the write path: today `edit*`/`sync*` do `native → SyncResult →
  _apply_graph_delta(result)`. Add, **immediately after** `_apply_graph_delta` (so
  the head graph already carries R's new `content_hashes`) and **within the same
  write boundary** (the Gate-3 §3.3.1 seam — do not release and re-acquire):
```python
def _invalidate_derived(self, result: SyncResult) -> None:
    if self._derivation is None: return                 # no derived layers ⇒ no-op
    dirty = set(result.created) | set(result.changed)   # DurableId-level; NOT the closure
    self._derivation.invalidate(self, dirty, set(result.deleted), result.revision)
```
- `DerivationDAG.invalidate(session, dirty, deleted, revision)`:
  - walk layers in **topological order** (so a layer-derived layer sees its upstream
    marked first);
  - for each layer, for each `id` in `dirty` it `applies_to`: compute the new
    `input_hash` (Step 4) and compare to the layer's **last-served binding**:
    - **unchanged ⇒ do nothing** (reuse — the cache hit; this is what kills over-fire
      even though `id` is in the delta);
    - **changed/missing ⇒ mark `(layer, id)` stale**, record the prior hash as
      *last-good* (for §8.2.5 serving), and (if `recompute == "eager"`) enqueue a
      recompute (Step 6);
  - for each `id` in `deleted`: drop the layer's binding for `id` (the artifact stays
    in the cache — it is content-addressed and may be reused if the entity returns;
    orphan GC is Step 9).
- **Precision, not closure:** `invalidate` consumes only `created ∪ changed ∪
  deleted`. It MUST NOT expand via reverse-deps (that would over-fire). The hash
  comparison is the precision mechanism.

**Validate.**
- Test (no over-fire, §8.3): edit entity `A`; `B` (an unrelated entity) and `C` (an
  *importer* of `A` whose own body is unchanged) are **not** marked stale; only `A`
  is. (This is the importer subtlety — assert `C` stays fresh.)
- Test (no miss): edit `A`'s body → `A` is stale; its new `input_hash` differs from
  the binding.
- Test (move): move `A` unchanged → `A` is **not** stale (hash unchanged).
- Test (cascade order): a config with `descriptions` → `description_embeddings`;
  changing `A` marks `descriptions[A]` stale before `description_embeddings[A]` is
  evaluated (topo order observable via a recorded trace).
- Test (in-boundary): after a write returns revision R, the staleness state already
  reflects R (no window where content is R but derived-staleness is R−1).
- **Acceptance gate:** all pass. Commit
  (`gate5: step 5 — precise delta invalidation, §8.3 no-miss/no-over-fire`).

---

## Step 6 — The recompute scheduler (idempotent, topo-ordered, fault-isolating)

**Goal.** Recompute stale artifacts in dependency order, at most once per
`(layer, input_hash)`, leaving the prior artifact intact on failure (§9.2.5, §9.2.6).

**Files.** `src/tyo3/derive/scheduler.py`, `src/tyo3/derive/dag.py`.

**Build.**
- A `RecomputeScheduler` with bounded worker(s). Work item = `(layer, durable_id,
  input_hash)`. **Idempotency (§9.2.5):** an in-flight/done set keyed by
  `(layer, input_hash)` ensures the same key computes **at most once**; scheduling a
  duplicate is dropped (two rapid deltas affecting the same entity ⇒ one recompute).
- **Topological gating:** a layer-derived item is not run until its upstream item for
  the same `id` has completed (or its upstream artifact is already fresh). The
  scheduler asks the DAG for upstream readiness.
- **Compute:** resolve input (Step 4) → `generator.generate([input])` → `cache.put(
  key, artifact)` → update the layer's binding for `id` to the new `input_hash` and
  clear stale. The cache `put` is idempotent per key (Step 1), so a racing duplicate
  is harmless.
- **Failure (§9.2.6):** a `GeneratorFailed` leaves the cache and the binding
  untouched (prior artifact intact) and marks `(layer, id)` `failed`; it is retryable
  on the next delta or read. Never write a partial artifact.
- **Policy:** `recompute == "eager"` enqueues on `invalidate` (Step 5);
  `recompute == "lazy"` enqueues on the first read miss (Step 7). Both go through the
  same idempotent scheduler.

**Validate.**
- Test (idempotent): enqueue the same `(layer, input_hash)` twice (or fire two deltas
  touching the same entity) → exactly one generator call.
- Test (topo): `descriptions` recomputes before `description_embeddings` for the same
  id; the embedding's input is the *fresh* description.
- Test (failure isolation): a generator that raises leaves the previous artifact
  readable and marks the target `failed`; a subsequent successful recompute clears it.
- Test (eager vs lazy): an `eager` layer recomputes without a read; a `lazy` layer
  recomputes only when first read.
- **Acceptance gate:** all pass. Commit.

---

## Step 7 — Snapshot derived reads (cross-layer join at R, honest staleness)

**Goal.** A snapshot resolves a derived value by the entity's content hash at R, and
reports staleness honestly (§8.2.5, §10.2.3, §10.2.2 derived half). This is what
makes Step 0's test pass.

**Files.** `src/tyo3/session.py` (the `Snapshot` class), `src/tyo3/models/derived.py`.

**Build.**
- `DerivedValue` model: `{ artifact: bytes, status: Literal["fresh","stale","failed","absent"], revision: int, layer: str }`.
- Add to `Snapshot`:
```python
def derived(self, layer: str, durable_id: str) -> DerivedValue:
    L = self._dag.layer(layer)
    input_hash = self._dag.resolve_input(L, self, durable_id)[1]   # via pinned graph @ R
    key = L.keys_for(input_hash)
    art = L.cache.get(key)
    if art is not None:
        return DerivedValue(art, "fresh", self.revision, layer)     # content-addressed ⇒ exact for R
    # miss at the current hash:
    if L.serving == "block":
        art = self._dag.recompute_now(L, self, durable_id)          # synchronous
        return DerivedValue(art, "fresh", self.revision, layer)
    # serving == "stale": serve last-good (prior hash) tagged stale, ensure a recompute is scheduled
    self._dag.schedule_lazy(L, self, durable_id)
    last = L.last_good_artifact(durable_id)
    return DerivedValue(last, "stale" if last else "absent", self.revision, layer)
```
- **Why this is exact for time-travel:** because the key is the content hash at R, a
  pinned snapshot at an *old* R whose entity hash still has an artifact returns it
  `fresh` with no head-state involvement — content-addressing gives cross-revision
  consistency for free (§8.2.2).
- Convenience accessors only if the layer exists: `snap.embedding(id)`,
  `snap.docstring(id)` → `derived("embeddings"/"docstrings", id)`.
- The pinned snapshot must read the **same `_dag`** the session owns (pass it into
  `Snapshot` like `head_graph_getter`); the cache and bindings are shared (the cache
  is immutable-by-key so this is safe across the snapshot/head boundary).

**Validate.**
- **Step 0's self-healing test now passes** (cache hit / recompute / reuse-on-move).
- Test (honest staleness): edit `A` with `serving="stale"`; immediately read
  `snap.derived(layer, A)` → status `stale`, returns last-good; after the scheduler
  finishes, a new snapshot reads `fresh`.
- Test (block policy): a `serving="block"` layer read computes synchronously and
  returns `fresh`.
- Test (time-travel): read `A` at an old retained R → `fresh` from the cache, no
  recompute, consistent with R's content.
- Test (cross-layer consistency, §10.2.2 derived half): at a pinned R,
  `snap.graph().node(id).content_hash`, `snap.derived("embeddings", id)`, and
  `snap.docstring(id)` all describe R.
- **Acceptance gate:** all pass; Step 0 green. Commit
  (`gate5: step 7 — snapshot derived reads, §8.2.5/§10.2.2`).

---

## Step 8 — Vector store, ANN delegation, and `snapshot.nearest`

**Goal.** Wire a real `VectorStore` backend and expose nearest-neighbour search at a
revision, delegating ANN entirely to the backend (§8.2.6).

**Files.** `src/tyo3/stores/lancedb_store.py` (or `sqlite_vec_store.py`),
`src/tyo3/session.py`.

**Build.**
- Implement the Gate-4 `VectorStore` protocol for one backend (lazily imported, per
  Gate 4 — `StoreBackendUnavailable` if the dep is absent). `put(key, vector_bytes)`
  inserts under the content-hash key + the vector; `nearest(q, k)` returns
  `[(key, score)]`. TyO3 stores the **hash↔vector linkage**, not the ANN index — the
  backend owns search.
- `Snapshot.nearest(query_vector, k)`: call `vector_store.nearest(q, k)` → keys →
  map each `input_hash` **back to the `DurableId`(s) at R** using the pinned graph's
  inverse index (`content_hashes[profile] → {id}` at R), dropping keys with no entity
  at R. Return `[(durable_id, score)]` consistent with R.
- Build the inverse `hash → {id}` index on the pinned graph once (small; reuse the
  node table). Two entities sharing a hash (true duplicates) both map back — return
  both, which is correct (they share the artifact, §7.3).

**Validate.**
- Test: insert vectors for a fixture; `snap.nearest(q, k)` returns `DurableId`s whose
  vectors are nearest; ids not present at R are excluded.
- Test: a duplicated entity (same hash in two files) maps the shared vector back to
  both ids.
- Test: the vector backend missing → `StoreBackendUnavailable` (lazy import).
- **Acceptance gate:** all pass. Commit.

---

## Step 9 — `generator_version` invalidation & orphan handling

**Goal.** A model/prompt change invalidates a layer's artifacts logically without
touching code or other layers (§8.2.4), and the deferred Gate-4 orphan-GC knob is
honoured.

**Files.** `src/tyo3/derive/dag.py`, `src/tyo3/derive/cache.py`.

**Build.**
- **`generator_version` bump (§8.2.4).** Because the key is `(input_hash,
  generator_version)`, bumping the version is automatically a **new key space**:
  subsequent reads miss and recompute; **prior keys remain** for rollback. No
  explicit purge is needed — verify and document this falls out of the key design.
  On `from_session`, if a layer's configured `generator_version` differs from what
  the cache last saw, no migration runs — the new space simply fills lazily/eagerly.
- **Orphan GC (Gate-4 `gc = "orphans"`).** Implement the deferred eviction pass: a
  `cache.gc(reachable_keys)` that deletes artifacts whose `(input_hash,
  generator_version)` is not referenced by any current entity at head **and** not the
  active `generator_version` (so rollback targets and live artifacts are kept).
  Default `gc = "never"` keeps everything. Run it only on explicit request
  (`session.gc()`), never implicitly in the write path.

**Validate.**
- Test: bump `generator_version`; a read recomputes under the new key; the old key is
  still present and readable (rollback).
- Test (`gc="orphans"`): after deleting entities and running `session.gc()`,
  unreferenced artifacts for non-active versions are removed; active and
  prior-version artifacts are kept.
- Test (`gc="never"`): `session.gc()` removes nothing for that store.
- **Acceptance gate:** all pass; `devenv shell -- tests` green. Commit.

---

## Gate 5 — Final acceptance (must all pass before Gate 6)

Run `devenv shell -- tests` (plus `devenv shell -- test-property` for the
precision fuzz) with a dedicated module proving:

1. **Content-addressed immutability (§8.2.1/§8.2.2).** Artifacts keyed by
   `(input_hash, generator_version)`; one immutable artifact per key; no revision
   awareness.
2. **No miss, no over-fire (§8.3).** Editing `X` recomputes only `X` (and genuinely
   hash-changed dependents); importers of `X` with unchanged bodies stay fresh.
3. **Reuse on move (§5.5.1 at the derived layer).** A moved-unchanged entity hits the
   cache.
4. **DAG correctness (§9).** Topo-ordered recompute; authored layers are sinks;
   idempotent scheduling (one recompute for duplicate triggers); failure leaves the
   prior artifact intact.
5. **Honest staleness (§8.2.5, §10.2.3).** Stale served tagged `stale` (default) or
   blocking (per policy), never silently fresh.
6. **Cross-layer consistency, derived half (§10.2.2).** At a pinned R, code hash,
   embedding (by hash), and docstring all describe R; time-travel reads are exact via
   content-addressing.
7. **`generator_version` invalidation (§8.2.4).** A bump yields a fresh key space with
   prior keys retained.
8. **ANN delegated (§8.2.6).** `snapshot.nearest` returns R-consistent `DurableId`s;
   the backend owns search.
9. **No-config no-op.** A project with no derived layers behaves exactly as Gate 4;
   the spine is unaffected.

When all nine pass on a clean `devenv shell -- tests`, tag the commit
`gate5-complete`. The first knowledge layer is in place and self-healing.

## Carrying forward to Gate 6 (MUST read before authored layers)

Gate 5 leaves the seams authored layers slot into:

- **Authored layers are DAG sinks (§9.2.4), already enforced.** Gate 4 rejects an
  authored layer used as a `depends_on`, and the Gate-5 `DerivationDAG` excludes
  authored layers as sources. Gate 6 registers authored layers **outside** the
  derivation DAG; nothing in this gate recomputes them.
- **The snapshot read surface extends, it does not change.** Gate 6 adds
  `snapshot.authored(layer, id)` alongside `snapshot.derived(...)`, resolving by
  `DurableId` (not content hash) against the authored store under `authored/<layer>/`
  (Gate 4 layout). Reuse the `_dag`/snapshot wiring pattern from Step 7.
- **Authored edits are a *write* (§5.4), unlike derived recompute.** A derived
  recompute fills a content-addressed cache and does **not** bump the revision; an
  authored edit flows through the commit transaction and **does** produce a revision
  + delta. Gate 6 adds that write type; Gate 5's invalidation already ignores
  authored layers, so an authored edit triggers no derived recompute (correct — they
  are sinks).
- **The Gate-2 `needs_review`/`orphaned` hooks drive authored `review_on_change`.**
  Reconciliation already surfaces `SyncResult.needs_review`/`orphaned`; Gate 6 binds
  authored records to those id lists so a changed entity flags its authored notes
  rather than dropping them.

## Sequencing & escalation notes

- **Step 0 before everything.** Freezing the public API (`snap.derived`, the
  generator counter seam) is what lets Steps 1–7 build toward a fixed target and
  proves self-healing at the end.
- **Never reintroduce the reverse-dep closure into derived invalidation.** Over-fire
  is the easy bug here; the hash comparison (Step 5) is the precision mechanism, and
  the closure belongs to the bus (Gate 8), not to the cache.
- **Keep the spine untouched beyond Step 1.** The only Rust change is per-profile
  hashes, and it is gated to the configured profiles so the no-config path is a
  no-op. If you find yourself adding revision awareness to the cache or a `DurableId`
  key to an artifact, stop — that contradicts §8.2.2 and breaks cross-agent reuse.
- **If layered derivation hashing gets subtle** (what exactly hashes for a
  layer-derived input), pin it to *the upstream artifact bytes* + the downstream
  `generator_version`; do not reach back to the original code hash, or you lose the
  self-healing property (an unchanged description must hit the embedding cache even
  when the code changed cosmetically).
- **Generators that call paid models** must batch and bound concurrency (Step 3 honours
  config); a missing/invalid credential is a Gate-4 `ConfigError` at load, not a
  runtime `GeneratorFailed`.
