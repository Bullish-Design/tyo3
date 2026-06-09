# Post-0.2.0 backlog — overview

> **Status:** open backlog. **Created:** 2026-06-08, on `v0.2.0` (spine refactor
> complete, full gate green, released).
> This is the consolidated record of everything still outstanding after the
> refactor — the genuine open items, plus pointers to the one already-planned and
> the one already-done. None block the library; it ships green at `v0.2.0`.

---

## 0. Status at a glance

| # | Item | Kind | Status |
|---|------|------|--------|
| 1 | FsStore garbage collection | bounded gap | **open** (this doc §1) |
| 2 | Symbol/entity walk context-struct | cleanup | **planned** → `.scratch/projects/18-symbol-entity-walk-refactor/OVERVIEW.md` |
| 3 | Optional store backends (qdrant, sqlite-vec) | feature | **open** (this doc §3) |
| 4 | Watcher-driven cross-file move detection | bounded gap | **open** (this doc §4) |
| 5 | Stale "deferred to Phase N" doc comments | cleanup | ✅ **done** (commit `7f8d30b`, released in 0.2.0) |

Suggested order if picking up: **#2** (pure mechanical, zero risk, already
planned) → **#1** (self-contained, well-understood fix) → **#4** (needs a focused
reconcile investigation) → **#3** (real feature, only when a backend is actually
wanted).

---

## 1. FsStore garbage collection

### What it is
Derived layers cache one artifact per entity in a *store*. The filesystem store
(`src/tyo3/stores/fs.py`) is content-hash-keyed: an artifact for cache key
`"<input_hash>:<generator_version>"` is written to

```
<cache_root>/<dd>/<dd>/<digest>      where digest = sha256(store_key).hexdigest()
```

"GC" = deleting artifacts whose key is no longer referenced by any live entity
(stale outputs from since-edited or deleted entities). Without it the on-disk
derived cache only grows. **Not a correctness bug** — stale files are simply
never read again — purely unbounded disk growth.

### Why it isn't implemented (the actual blocker)
The on-disk filename is **`sha256(store_key)`** — a *one-way* hash. The current
GC code (`derive/dag.py::_gc_store`) walks the cache directory, takes each
on-disk `digest` (`parts[2]`), and tries to recover the store key from it to test
reachability. **You can't invert sha256**, so it can't match disk files to the
reachable-key set and bails (`pass`). `ArtifactCache.gc` (`derive/cache.py:75`)
is likewise a documented no-op.

### The fix (forward-hash, mark-and-sweep)
The inversion is unnecessary. `gc_orphans` already computes the set of **reachable
store keys** (`{f"{h}:{version}"}` over every live `content_hash`). Hash those
*forward* to get the reachable **digest** set, enumerate the disk, and delete any
digest not in it:

1. Add a method to the `Store` protocol (`stores/base.py`):
   ```python
   def prune(self, reachable_keys: set[str]) -> int: ...   # returns #deleted
   ```
   Each backend owns its own layout, so reachability lives behind the abstraction
   (the dag layer must not know FsStore's digest scheme — that coupling is the
   current bug's root).
2. `FsStore.prune`: `reachable_digests = {sha256(k.encode()).hexdigest() for k in
   reachable_keys}`; `for f in root.rglob("*")` (skip `*.tmp`): if `f.is_file()`
   and `f.name not in reachable_digests`, `unlink()`. Prune now-empty shard dirs.
3. Replace `_gc_store`'s broken directory-inversion with `store.prune(reachable_keys)`.
4. `VectorStore`/lancedb: a `prune` that deletes rows whose id ∉ reachable_keys
   (or leave `gc != "orphans"` layers untouched, as today).

### The one real design question: *reachability across revisions*
`gc_orphans` currently builds reachable keys from **the current head graph only**.
But snapshots time-travel and `serving = "stale"` layers keep last-good artifacts,
so an artifact reachable from a *retained* (non-head) revision would be wrongly
pruned. Decide the policy:
- **Conservative (recommended first):** union reachable hashes across the store's
  retained-revision window (and/or each layer's `_last_good` bindings), so GC
  only reaps artifacts unreachable from *any* live revision.
- Keep the existing `gc = "orphans"` opt-in per store, and the
  cross-generator_version retention (other versions preserved for rollback) — GC
  only ever prunes within the active version space.

### Files
- `src/tyo3/stores/base.py` (protocol), `src/tyo3/stores/fs.py` (impl),
  `src/tyo3/stores/lancedb_store.py` (impl), `src/tyo3/derive/cache.py`
  (`ArtifactCache.gc` → delegate to `store.prune`), `src/tyo3/derive/dag.py`
  (`gc_orphans` / delete the broken `_gc_store` inversion).

### Acceptance
- A unit test: populate a layer, edit an entity so its hash changes (old artifact
  now unreachable), run `session.gc()` (or the dag GC), assert the old digest file
  is gone and every reachable artifact survives — and that a time-travel read of
  the *old* snapshot still works if the policy says it should.
- `gc = "orphans"` honoured; non-orphan stores untouched; other
  generator_versions retained.

### Risk
Over-pruning an artifact a retained snapshot still needs → wrong derived read on
time-travel. The reachability-union policy above is the guard; test it explicitly.

---

## 2. Symbol/entity walk context-struct cleanup → see project 18

Fully planned in `.scratch/projects/18-symbol-entity-walk-refactor/OVERVIEW.md`.
Two recursive Rust walks (`collect_symbols_recursive`,
`collect_entities_recursive`) thread a fixed analysis context through 12–13
params; fold it into a `…WalkCtx<'a>` struct and drop the two
`#[allow(clippy::too_many_arguments)]`. Behaviour-preserving; not started.
Not duplicated here.

---

## 3. Optional store backends (qdrant, sqlite-vec)

### What it is
A derived layer's `store` declares a `backend`. The registry
(`src/tyo3/stores/__init__.py::open_store`) wires:
- `fs` — implemented (`FsStore`).
- `lancedb` — implemented (`LanceDbStore`, a `VectorStore`).
- `qdrant` (`qdrant_client`) and `sqlite-vec` (`sqlite_vec`) — listed in
  `_OPTIONAL_BACKENDS`, but **no adapter exists**: even with the package
  installed, `open_store` raises
  `StoreBackendUnavailable("… adapter is not implemented in Gate 4")`.

### What's needed
Implement a `VectorStore` adapter per backend (the `Store` protocol is tiny:
`get`/`put`/`has`/`delete`, plus `add`/`nearest` for `VectorStore`, plus `prune`
once §1 lands):
- `src/tyo3/stores/qdrant_store.py` — wrap `qdrant_client`; collection per layer;
  `put`/`get` by point id = store key; `nearest` via search.
- `src/tyo3/stores/sqlite_vec_store.py` — `sqlite_vec` virtual table; rows keyed
  by store key.
Then add the `backend == "qdrant" / "sqlite-vec"` arms to `open_store` (mirroring
the `lancedb` arm: import-guard → construct), and drop those keys from the
"adapter not implemented" fall-through.

### Files
- New: `src/tyo3/stores/qdrant_store.py`, `src/tyo3/stores/sqlite_vec_store.py`.
- `src/tyo3/stores/__init__.py` (`open_store` arms).
- Tests mirroring `test_gate5_derived.py::TestVectorStore` (skip-if-package-missing).

### Acceptance
- With the optional package installed, a derived layer configured for the backend
  round-trips `put`/`get`/`nearest`; without it, `StoreBackendUnavailable` with a
  clear "requires optional package" message (already the case).

### Note
This is a **real feature**, gated on actually wanting that backend — lowest
priority unless a consumer needs vector search on qdrant/sqlite-vec specifically.
lancedb already covers the vector-store path end to end.

---

## 4. Watcher-driven cross-file move detection

### What it is
Moving an entity to a new file with an unchanged body should preserve its
`DurableId` and be reported as one **`moved`** entry (same id, new location) —
not a delete + a create. Two paths reach reconciliation:
- **Commit path** (`session.edit_many({old: "", new: <body>})`) — **works**: the
  reconciler sees the delete + create in one scope and applies the §5.5 rule-2
  hash-match (same content hash, different path ⇒ `Moved`).
- **Watcher path** (`poll_changes` ingesting disk events: a delete of `a.py` + a
  create of `b.py` with identical content) — **gap**: the scoped reconcile
  processes both changes but reports delete + create rather than the single
  hash-matched move.

### Current status
Documented, non-strict xfail at
`src/tyo3/graph/tests/test_incremental_parity.py:347`
(`test_moved_entity_preserves_id_and_updates_location`). It is **outside** the
gated `testpaths` (`src/tyo3/tests`), so it neither blocks the suite nor
XPASS-fails. Phase 5 already fixed the *other* half (the watcher no longer drops
the delete+create batch — see `test_watch`); what remains is move *detection*
over that batch.

### Where the fix lives (needs a focused investigation)
Identity-layer move detection (`reconcile` / `reconcile_scoped` in
`rust/src/identity.rs`, §5.5 rule 2). The likely divergence: the commit path
reconciles the deleted and created entities together so the hash-match across
them fires; the watcher's scoped reconcile either scopes per-file or doesn't
cross-reference a just-deleted entity's content hash against a just-created one in
the same poll batch. The fix is to make the watcher-batch reconcile consider
hash-matched moves across the whole batch, exactly as the commit funnel does —
ideally by routing both through the same reconcile entry so there is one move-
detection rule, not two.

### Files
- `rust/src/identity.rs` (`reconcile_scoped` / the move-detection rule),
  `rust/src/project/*` (the `poll_changes` → reconcile wiring vs the commit →
  reconcile wiring).
- Flip the xfail to a real assertion once it passes; consider promoting it into
  the gated `testpaths`.

### Acceptance
- The xfail test passes as a normal test: a watcher-delivered cross-file move
  preserves the id and reports one `moved` entry; `locate(id)` returns the new
  path. Commit-path move behaviour unchanged.

### Risk
Low blast radius but identity-sensitive: a too-eager hash-match could mis-bind two
genuinely-distinct same-hash entities. The existing `needs_review` ambiguity
handling (§5.5) is the guard; make sure a watcher-batch move respects it.

---

## 5. Stale doc comments — done

The "deferred to Phase N / wired but idle / empty until Phase 4" comments that
misstated current behaviour were swept in the 0.2.0 documentation pass
(`rust/src/project.rs`, `code_layer.rs`, `dto/commit_delta.rs`,
`project/commit.rs`, `models/delta.py`, `views.py`) and the README was expanded to
describe the incremental engine. Commit `7f8d30b`, released in `0.2.0`. Listed
here only for completeness.

---

## References
- `.scratch/projects/18-symbol-entity-walk-refactor/OVERVIEW.md` (#2).
- `.scratch/projects/16-refined-implementation-work/PROGRESS.md` §9.14 (refactor
  close-out), §17 (xfail ledger — gated paths now zero; the watcher move xfail is
  the one ungated exception).
- Memory: `[[phase14-acceptance-done]]`.
