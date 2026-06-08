# Phase 8 (V2) — Unified derived invalidation (per-layer key locality)

> Execution guide for **Phase 8 of `REFINED_IMPLEMENTATION_PLAN_V2.md`**. Read
> Concept V2 §5.5 (the unified invalidation model) and V1 §5.7 (the
> content-hash-keyed cache), §5.8 (the derivation DAG), §5.9 (cross-layer
> consistency), §5.12 (fail loudly) once before starting.
>
> **This is the V1 "Phase 7 — repair derived layers" work, re-conceived** around
> the V2 affected model. The producer (Phase 6) now hands derived invalidation a
> genuinely transitive, container-granular `affected` set. The remaining task is
> to make invalidation *precise per layer*: a **local** layer must not recompute
> when only a dependency changed, and a **semantic** layer must.
>
> **Depends on Phases 6–7.** `affected_ids` must be transitive at the source and
> the bus delta a pure projection. Phase 6 also gave us one `_after_commit` hook
> calling `_schedule_derived(delta)` — Phase 8 fixes what that calls into.

---

## 0. Dev environment

All commands via `devenv shell --`. **Pure Python** (`src/tyo3/derive/`,
`src/tyo3/stores/`) — no Rust, no rebuild needed in the inner loop. Run
`devenv shell -- build` once before the final gate. Suites are slow.

---

## 1. What Phase 8 changes, and why

Today derived invalidation is effectively inert (it was fed path-shaped data; the
per-id hash lookup raised and was swallowed) and the eager recompute leaked a
snapshot. Phase 6 fixed the *input* (id-level transitive `affected`). Phase 8
fixes the *mechanism* and adds the V2 key-locality model:

1. **Per-layer key locality (Concept V2 §5.5).** Each derived layer declares
   whether it is **local** (key = `content_hash`) or **semantic** (key =
   `content_hash + dependency-closure fingerprint`). This is what resolves the
   long-standing tension between content-hash keying and transitive affected.
2. **One affected-driven loop.** For each id in `affected`, recompute the layer's
   key; changed ⇒ stale ⇒ schedule. Local layers no-op on
   `affected`-without-`changed` ids (no over-recompute); semantic layers recompute
   on any dependency change.
3. **Read-time staleness** replaces transaction-time mutable flags.
4. **One pinned snapshot** for invalidation + recompute, closed in `finally`.
5. **Typed store errors** (absent vs backend-unavailable vs backend-broken).

### Files in scope

| File | Role |
|---|---|
| `src/tyo3/derive/` (DAG, scheduler, layer model, cache) | **edit (core)** — key-locality declaration; one affected-driven loop; read-time staleness; snapshot lifetime (8.1–8.4) |
| `src/tyo3/stores/` | **edit** — typed errors; stop swallowing IO/query failures (8.5) |
| `src/tyo3/session.py` | **edit** — `_schedule_derived(delta)` feeds the id-level loop (8.2) |
| `src/tyo3/tests/test_final_derived_contract.py`, `test_gate5_derived.py` | un-skip self-healing; add local-vs-semantic tests (8.6) |

---

## 2. Working rules

1. **Key locality is declared, not inferred.** A layer states `local` or
   `semantic` in its registration/config. Default `local` (the common case).
2. **Local layers never recompute on dependency-only changes.** Prove it: an edit
   that changes a *dependency* of entity E (so E ∈ `affected` but E ∉ `changed`)
   must not recompute E's local artifact.
3. **Semantic layers recompute on dependency changes.** Prove the converse.
4. **No mutable transaction-time derived state.** Staleness is a read-time key
   comparison at the snapshot.
5. **No snapshot leaks.** One pinned snapshot, closed in `finally`.
6. **Stores fail loudly.** "Missing" only for a genuinely absent artifact; all
   other IO/query errors propagate as typed errors.

---

## 3. Step-by-step

### Step 8.1 — Declare per-layer key locality

**Where / how.** Add a `key_locality: Literal["local","semantic"]` (or an enum)
to the derived-layer descriptor. The cache key is computed by the layer:
- `local`: `key = (content_hash, generator_version)`.
- `semantic`: `key = (content_hash, dependency_fingerprint, generator_version)`,
  where `dependency_fingerprint` is a stable hash of the content hashes of the
  entity's dependency closure (use the producer's forward edges / `reverse_deps`
  inverse at the pinned snapshot — id→deps→their content hashes, sorted).

**Verify.** `devenv shell -- pytest src/tyo3/tests/test_gate5_derived.py -q --no-cov`

### Step 8.2 — One affected-driven invalidation loop

**Where / how.** `_schedule_derived(delta)` builds the candidate set from
`delta.affected_ids` (it is the transitive container-granular closure). For each
layer and each affected id: compute the layer key at the new revision; if it
differs from the cached key, mark stale and schedule recompute. Walk the
derivation DAG in topological order (V1 §5.8); authored layers are sinks
(reactions terminate); schedule each `(layer, key)` at most once.

A **local** layer's key only moves when the id's own `content_hash` moved — so
affected-but-unchanged ids are no-ops. A **semantic** layer's key moves when any
dependency's content hash moved — so it recomputes. One loop, two behaviors,
selected by the declared locality.

**Verify.** `devenv shell -- pytest src/tyo3/tests/test_final_derived_contract.py -q --no-cov -rA`

### Step 8.3 — Read-time staleness

**Where / how.** For a `(layer, id)` at a snapshot: resolve the id's content hash
under the layer's hash profile → form the key → if the artifact exists it is
fresh; if missing but a last-good exists it is stale/failed; if nothing exists it
is absent or blocks per the layer's serving policy. Remove transaction-time
mutable "dirty/stale" flags — staleness is a pure read comparison.

### Step 8.4 — Fix the recompute snapshot lifetime

**Where / how.** The derivation pass uses **one** pinned snapshot for both
invalidation and eager recompute, closed in a `finally`. Delete the path that
opens a second snapshot and never closes it.

### Step 8.5 — Typed store errors

**Where / how.** A filesystem store returns "missing" only for a genuinely absent
file and propagates all other IO errors typed. The vector store stops swallowing
query/write errors. A missing optional backend is a distinct typed "backend
unavailable," separable from "not found" and "backend broken" (V1 §5.12).

### Step 8.6 — Complete the self-healing + locality tests

Un-skip the self-healing derived test (unrelated edit ⇒ no recompute; content
change ⇒ recompute; move ⇒ artifact reused; generator-version bump ⇒ new
key-space; failure ⇒ last-good kept). Add two new tests:
- **local-no-recompute:** edit a dependency of E; assert E's *local* artifact is
  not recomputed (E ∈ affected, E ∉ changed).
- **semantic-recompute:** same edit; assert E's *semantic* artifact recomputes.

> Commit here: `fix(derived): unified affected-driven invalidation with per-layer key locality`.

---

## 4. Acceptance

```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_derived_contract.py src/tyo3/tests/test_gate5_derived.py -q --no-cov -rA
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria:** invalidation is id-level and driven by transitive `affected`;
local layers no-op on dependency-only changes, semantic layers recompute; no
mutable transaction-time derived state; no snapshot leaks; typed store errors; the
self-healing test and the two locality tests are green.

---

## 5. Pitfalls

- **Treating all layers as semantic.** Over-recomputes every local artifact on any
  transitive change — the over-fire we deliberately avoid. Default `local`.
- **Computing the dependency fingerprint from the head graph.** Use the *pinned
  snapshot* — head-time reads are racy and can take the write lock (V1 §5.9).
- **Leaving the second-snapshot leak.** Grep for snapshot opens without a matching
  close in the derive pass.
- **Swallowing store errors as "missing."** Reintroduces the silent-no-op class.

---

## 6. What Phase 8 leaves for later

- **Method-level precision of the affected set** — Phase 9 (the async refinement
  narrows `affected`; the derived loop will simply consume the narrowed set when a
  refinement arrives).
- **Read-surface / convenience-view lifetime** — Phase 11.
