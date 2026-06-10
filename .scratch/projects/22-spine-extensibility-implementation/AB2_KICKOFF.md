# KICKOFF — PR8 = AB2: Producer protocol + traced read-sets

You are continuing the **TyO3 spine-extensibility build**. Phase A (4 QW PRs) and
all of Phase B (**AB1** registration keystone, **AB5** schemas, **AB7**
subscriptions) are **landed and MERGED into `nvim-plugin`** (@ `3f3f4f6`). AB2 is
the **first Phase-C PR** and the load-bearing one: it makes the registration API
AB1 shipped actually usable for the most valuable layer kind — an **expensive
reverse** layer (references / callsite-summary style) that is *stale-forever*
today. Start coding at Task 1.

This is a **pure-Python PR (no Rust, no `build`)**. The `Producer` /
`ProduceContext` protocols were **declared in AB1** (`extend.py:153-188`,
`@runtime_checkable`) but left **unwired** — AB2 wires them. Do **not** redeclare
them; implement against them.

---

## State on entry (verify, do NOT redo)

- **Phase B merged.** `nvim-plugin` @ `3f3f4f6` contains AB1+AB5+AB7. PR #7 MERGED;
  #8/#9 CLOSED-as-merged (commits in `nvim-plugin`). Umbrella **PR #2
  (nvim-plugin → main) is still open — do not touch it.**
- **Branch:** `git checkout nvim-plugin && git pull && git checkout -b
  spine-extend-ab2`. Open the PR with **base `nvim-plugin`**.
- **The protocols already exist** (`extend.py`): `ProduceContext` (`:153`) with
  `durable_id/kind/source/location/snapshot` + `find_references` / `symbol` /
  `dependents` / `dependencies` / `upstream` / `note_read`; `Producer` (`:176`)
  with `produce(ctxs) -> list[Any]` + optional `setup`/`teardown`. They are typed
  on `DerivedLayerSpec.produce: Producer | Generator | str` (`:110`). **Reuse —
  don't reinvent.**

## Read first (in order)
1. `21-.../IMPLEMENTATION_GUIDE.md` → **"TASK AB2"** (`:464`) — the 5-step spec +
   §0 golden rules + §0.1 the devenv loop. (Its line anchors drift; trust the
   "exact shape" below, grounded in post-merge source.)
2. `API_DESIGN.md` **§2.4** (`:160`, the recording-handle widening + why traced
   read-sets are salsa's own model) and **§5.2b** (`:355`, the callsite-summary
   expensive-reverse worked example — where caching *is* worth it).
3. `SPIKE_FINDINGS.md` **Spike C** (the stale-forever result a references-style
   layer gets under forward/`semantic` fingerprinting — the bug AB2 fixes) and
   the **Appendix A taxonomy** (cheap-reverse → live RPC, already shipped as QW1;
   expensive-reverse → traced read-set keying, this PR's default).
4. `spikes/spike_c2.py` — port it as the AB2 contract test (now possible with a
   real producer).
5. The `spine-extensibility-implementation` memory (chain state, golden rules).

---

## The core gap

Today a derived **producer gets only entity text**: `Generator.generate(inputs:
list[GenInput]) -> list[bytes]` (`generators.py:45`), where `GenInput` carries
`{durable_id, source, kind, meta}`. There is **no way for a producer to read other
entities**, so a *reverse* layer (e.g. "summarize this function's callsites") can't
be written at all — and even if it could, the cache key is fingerprinted from the
entity's **own** content (or its forward dependency closure for `semantic`), so a
new *caller* never invalidates it → **stale-forever** (Spike C).

AB2 closes this with a **recording read handle**. The producer reads the pinned
snapshot through a `ProduceContext`; every id it touches is logged into a per-call
**read-set**; the framework fingerprints *exactly that set* into the cache key. A
lazy read then self-heals whenever **any traced id** changes — forward, reverse,
sibling, or mixed — **by construction, with no direction enum to get wrong** (this
is salsa's dependency model, apt because ty/salsa is the engine underneath).
`local` / `semantic` / `reverse-semantic` stay as fast-path overrides for
producers that opt out of tracing.

---

## The exact shape (grounded in post-merge source @ 3f3f4f6)

### Task 1 — the recording `ProduceContext` (`session/views.py`)
Implement a concrete `_RecordingContext` wrapping a pinned `Snapshot` (`views.py`
`Snapshot` is at `:76`). On each `find_references` / `symbol` / `dependents` /
`dependencies` / `upstream` call, resolve via the snapshot's existing `_ReadOps`
surface **and append the resolved ids to `self.read_set: set[str]`**. Expose the
raw `snapshot` attribute as the escape hatch; `note_read(ids)` adds to the read-set
explicitly for reads done off the raw handle. **It reads the frozen snapshot only
(rule #2) — never the live head.** One context per `(durable_id)` in the batch.

### Task 2 — define the legacy adapter (`derive/generators.py` + `extend.py`)
Adapt the existing batched generators to the `Producer` contract: a
`_GeneratorProducer` whose `produce(ctxs)` calls the wrapped
`Generator.generate([GenInput(...) for ctx in ctxs])` and, for each ctx, does
`ctx.note_read([ctx.durable_id])` so its **read-set is exactly `{durable_id}`** ⇒
it keys identically to `local` today. The `python`/`command`/`http` built-ins
(`generators.py:72/:117/:195`) ride this wrapper unchanged. `setup`/`teardown` are
no-ops for them. **Assert in a test that this produces byte-identical keys to the
pre-AB2 `local` path (no behaviour change).**

### Task 3 — key on the traced set (`derive/dag.py`)
`DerivationDAG.resolve_input` (`:229`) currently computes the key **before**
producing (own content hash, or `semantic` folds in `_dependency_fingerprint`,
`:256-258`). A traced read-set is only known by **running the producer** — so for a
**default (traced)** layer the key is computed *post-hoc* (salsa model). Restructure
the code-derived path so that for traced layers you:
1. build the `_RecordingContext`(s), run `producer.produce(ctxs)`, collect each
   ctx's `read_set`;
2. compute `input_hash = _hash_bytes(serialize(sorted((id,
   snapshot.graph().symbol(id).content_hashes[profile]) for id in read_set)))`;
3. cache the produced artifact under that key.

Keep `local` (own content hash, `:260`), `semantic` (reuse `_dependency_fingerprint`,
`:257`), and add a **`reverse-semantic`** override (direct **reverse** edges only —
reuse the maintained reverse-dep index, lazy + memoised per snapshot) as explicit
fast-path branches. This means the traced strategy interleaves produce+key, whereas
the override strategies key-then-produce as today — make that split clean (the
caller in `scheduler.recompute_now`, reached from `Snapshot.derived` `:211`/`:250`,
must handle both). **The central design tension of this PR is exactly this
produce-then-key inversion — plan it before you type.**

### Task 4 — lifecycle + registration wiring (`derive/dag.py`, `extend.py`)
- `Producer.setup()` once when the DAG builds the layer (`DerivationDAG.from_session`,
  `:40`); `teardown()` on `session.close()`.
- A `DerivedLayerSpec.produce` that is a **`Producer` object** uses the new
  recording path; a **dotted string** keeps the legacy adapter via the existing
  `resolve_generator` (`extend.py:294`). Default `key_locality=None` ⇒ traced.

### Task 5 — tests (`src/tyo3/tests/test_registration.py` + a Spike-C port)
- **Port `spike_c2.py`** as a contract test with a *real references producer*
  (now possible): prime a `refs`/callsite layer on the callee, **add a new caller**,
  re-read → assert the producer **recomputed** and the value reflects the new caller
  (the inverse of today's stale-forever). Use the `_clean_registries` autouse
  fixture; any pydantic helper model carries `__test__ = False`.
- **No-regression:** assert a `local`-style (legacy-adapter) producer does **not**
  recompute on a dependency-only change, and keys identically to pre-AB2.

---

## Locked decisions (do NOT re-litigate)
- **Traced read-set is the DEFAULT** for the new protocol; `local`/`semantic`/
  `reverse-semantic` are opt-out fast paths. No direction enum on the traced path.
- **Wire the AB1-declared protocols; don't redeclare them.** Producer output may be
  `bytes | BaseModel | None`; typed-return validation is a later concern (rides
  AB2's path but isn't this PR's gate — match the guide).
- **Legacy generators become adapters** with read-set `{durable_id}` ⇒ byte-identical
  keys to `local`. Config stays valid; the `python`/`command`/`http` names keep
  working with zero behaviour change.
- **No Rust.** AB2 is pure-Python over the existing frozen-snapshot read surface.
- **Cheap-reverse stays live RPC** (QW1, already shipped) — AB2 is *expensive*-reverse
  only. Don't move references/diagnostics into a cached layer.

## Non-obvious gotchas
- **Produce-then-key inversion** (Task 3) is the whole hazard: the traced key isn't
  knowable until the producer runs, so traced layers cannot reuse the
  key-then-fetch shortcut. Keep the override branches on the old path; only the
  traced branch interleaves.
- **`reverse-semantic` = direct reverse edges only.** Do **not** fingerprint the
  *transitive* reverse cone — that thrashes (every upstream edit reuses half the
  graph). Memoise the direct-reverse lookup per snapshot.
- **No eager reverse recompute.** Self-healing is lazy-at-read; adding a caller does
  not eagerly recompute the callee's summary — the next *read* of it does.
- **Recording reads the frozen snapshot** (rule #2). The `_RecordingContext` must
  never reach the live head, or you reintroduce read-side staleness.
- **`note_read` is the escape hatch** for the raw `snapshot` attribute — reads
  through the typed methods auto-trace, raw-handle reads do not.

## Anchor lines (post-merge, on `nvim-plugin` @ 3f3f4f6)
- `src/tyo3/extend.py:153` `ProduceContext`, `:176` `Producer` (declared, wire these),
  `:110` `DerivedLayerSpec.produce`, `:294` `resolve_generator` (legacy dispatch).
- `src/tyo3/derive/generators.py:42` `Generator`, `:45` `generate`, `:72/:117/:195`
  the `Python`/`Command`/`Http` generators, `:53` `make_generator`.
- `src/tyo3/derive/dag.py:229` `resolve_input` (the key seam; `:256` semantic branch,
  `:260` local branch), `:40` `from_session` (setup hook).
- `src/tyo3/session/views.py:76` `Snapshot`, `:211` `Snapshot.derived`, `:250`
  `scheduler.recompute_now` call site (the produce path to thread the context through).
- Design: `API_DESIGN.md:160` §2.4, `:355` §5.2b; `SPIKE_FINDINGS.md` Spike C +
  Appendix A; `spikes/spike_c2.py`.

## Ground rules (non-negotiable)
- **Everything through devenv.** Pure-Python ⇒ **no `build`**. Inner loop:
  `devenv shell -- test-fast`. Before the PR (inside devenv): `pytest
  src/tyo3/tests/test_registration.py --no-cov`, `pytest src/tyo3/daemon/tests
  --no-cov`, `test-final`, and `ruff check` on touched files. Suites are slow —
  background, budget ~15 min.
- **Golden invariants:** Rust owns committed truth (AB2 adds **no** native change);
  reads over a frozen snapshot; one writer via `SessionActor`; don't touch the
  parity oracle or `[profile.dev.package."*"]`; identity rules unchanged.
- **No AI attribution** anywhere (commits, PRs, code, docs).

## Baseline (on `nvim-plugin` @ 3f3f4f6 — all fails are environmental)
`test-fast` → **~696 pass / 1 fail**; the fail is the documented
`test_sidecar_is_sole_path_owner_in_source` (demo/tour.py). An xdist shared-fixture
flake may also appear that **passes serially** (`-p no:xdist`). daemon **49/49**;
Lua smoke **12/12**; `test-final` **43/43**. AB2 only adds passes.

## Definition of done (PR8)
The `Producer`/`ProduceContext` protocols are **wired**: a producer reads the
frozen snapshot through a recording context whose traced read-set is fingerprinted
into the cache key (default strategy); legacy generators ride a `{durable_id}`
adapter keying identically to `local`; `local`/`semantic`/`reverse-semantic`
override branches intact; `setup`/`teardown` lifecycle honoured; an
**expensive-reverse layer self-heals at read when a caller is added** (Spike-C
port green) and a `local`-style producer shows no regression. `test-fast` green
(baseline only); daemon + `test_registration.py` + `test-final` green; ruff clean;
`architecture.md` documents the producer protocol + traced read-set keying (and
the cheap-vs-expensive-reverse taxonomy); `PROGRESS.md` (mark PR8 landed) + the
`spine-extensibility-implementation` memory updated; push `spine-extend-ab2`, open
a PR (base `nvim-plugin`; no AI attribution). Then stop and report.

> **Note for the planner:** AB3 (async serve for slow producers) builds directly
> on this — keep the produce path reentrant/enqueue-able. AB4 (read concurrency)
> and AB8 (native rename) are **behind a human-review gate** — do NOT start them.

## Memories to recall
`spine-extensibility-implementation`, `spine-extensibility-review`,
`session-reads-via-frozen-snapshot`, `phase8-derived-invalidation-done`,
`phase9-precision-refinement-done`, `devenv-test-entrypoints`,
`test-run-timeouts`, `commit-no-ai-attribution`.
