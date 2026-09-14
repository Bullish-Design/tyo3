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
| 1 | FsStore garbage collection | bounded gap | ✅ **done** |
| 2 | Symbol/entity walk context-struct | cleanup | **planned** → `.scratch/projects/18-symbol-entity-walk-refactor/OVERVIEW.md` |
| 3 | Optional store backends (qdrant, sqlite-vec) | feature | **open** (this doc §3) |
| 4 | Watcher-driven cross-file move detection | bounded gap | ✅ **done** (verified on current `main`) |
| 5 | Stale "deferred to Phase N" doc comments | cleanup | ✅ **done** (commit `7f8d30b`, released in 0.2.0) |

Suggested next item: **#3** (real feature, only when a backend is actually wanted).

---

## 1. FsStore garbage collection ✅ done

### What it is
Derived layers cache one artifact per entity in a *store*. The filesystem store
(`src/tyo3/stores/fs.py`) is content-hash-keyed: an artifact for cache key
`"<input_hash>:<generator_version>"` is written to

```
<cache_root>/<version_enc>/<input_hash[:2]>/<input_hash>
```

"GC" = deleting artifacts whose key is no longer referenced by any live entity
(stale outputs from since-edited or deleted entities). Without it the on-disk
derived cache only grows. **Not a correctness bug** — stale files are simply
never read again — purely unbounded disk growth.

### What was missing
GC was implemented by `ArtifactCache` in terms of the filesystem store's
`iter_keys` helper, so the cache layer owned backend layout knowledge and vector
stores could not participate. The DAG also considered only the current HEAD,
which could remove an artifact still needed by a retained time-travel revision.

### The fix (implemented)
Pruning is now owned by each store backend:

1. `Store.prune(reachable_keys)` is part of the store protocol and returns the
   number of deleted artifacts.
2. `FsStore.prune` enumerates its structural key layout, deletes only
   unreachable keys in the supplied generator-version spaces, preserves other
   versions for rollback, and removes empty shard directories.
3. `ArtifactCache.gc` delegates to `Store.prune`; the DAG no longer knows how a
   backend enumerates artifacts.
4. `LanceDbStore.prune` applies the same version-scoped reachability rule to
   rows.

The protocol shape is:

   ```python
   def prune(self, reachable_keys: set[str]) -> int: ...
   ```
The DAG unions reachability across every retained MVCC revision and each
layer's last-good bindings. Traced layers are conservatively skipped because
their historical producer read-set keys cannot be reconstructed without
rerunning user code. Empty reachability remains a safe no-op.

### Policy
The conservative retained-revision policy is implemented: GC only reaps
artifacts unreachable from every live revision in the retention window, while
`gc = "orphans"` remains opt-in and other generator versions remain intact.

### Files
- `src/tyo3/stores/base.py` (protocol), `src/tyo3/stores/fs.py` (impl),
  `src/tyo3/stores/lancedb_store.py` (impl), `src/tyo3/derive/cache.py`
  (`ArtifactCache.gc` → delegate to `store.prune`), `src/tyo3/derive/dag.py`
  (`gc_orphans` / delete the broken `_gc_store` inversion).

### Acceptance
- Unit coverage proves orphan deletion, reachable-artifact retention,
  generator-version isolation, temporary-write preservation, shard cleanup, and
  time-travel reads from retained snapshots.
- `gc = "orphans"` is honoured; non-orphan stores remain untouched.

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

## 4. Watcher-driven cross-file move detection ✅ done

### What it is
Moving an entity to a new file with an unchanged body should preserve its
`DurableId` and be reported as one **`moved`** entry (same id, new location) —
not a delete + a create. Two paths reach reconciliation:
- **Commit path** (`session.edit_many({old: "", new: <body>})`) — **works**: the
  reconciler sees the delete + create in one scope and applies the §5.5 rule-2
  hash-match (same content hash, different path ⇒ `Moved`).
- **Watcher path** (`poll_changes` ingesting disk events: a delete of `a.py` + a
  create of `b.py` with identical content) — **verified working**: the scoped
  reconcile processes both changes together and reports the single hash-matched
  move.

### Current status
The gap is already resolved on the current `main` checkout; no implementation
change was needed. The old reference to
`src/tyo3/graph/tests/test_incremental_parity.py` is stale: that file is not in
this checkout, and the durable regression now lives in the gated top-level
`tests/test_watch.py` suite.

The deterministic injection test and a real-watcher rename test both verify one
revision, one structured move, preserved `DurableId`, empty `created_ids` /
`deleted_ids`, and `locate()` at the destination. The path-shaped `created` /
`deleted` metadata continues to list the filesystem paths by design.

### Why it works
The existing shared identity/commit path already provides the needed behavior.
`poll_changes()` drains the complete watcher batch into `Mutation::Poll`,
`build_poll_plan()` creates one scope containing both file paths,
`extract_entities_for()` extracts the destination entity, and
`reconcile_scoped()` retains the deleted anchor in `by_hash` until Pass B can
match the unchanged body. Both commit and watcher changes therefore use the
same hash-based move rule in `rust/src/identity.rs`.

### Files
- `tests/test_watch.py` (deterministic and real-watcher regression coverage).
- `rust/src/identity.rs` and `rust/src/project/commit.rs` were inspected and
  left unchanged; the shared reconciliation funnel is already correct.

### Acceptance
- Verified: a watcher-delivered cross-file move preserves the id and reports one
  `moved` entry; `locate(id)` returns the new path. Commit-path move behavior is
  unchanged.

### Risk addressed
The existing `needs_review` ambiguity handling (§5.5), one-to-one consumed sets,
and deterministic ordering remain in force; the watcher uses the same
hash-based move rule as the commit path.

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
