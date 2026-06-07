# Gate 3N — Architectural Analysis: Should We Implement It?

> **Date:** 2026-06-07  
> **Status:** Analysis of whether to resurrect Gate 3N (Rust-side code-layer delta
> production) on top of the current `HEAD` (post Gate 8 complete).

---

## The question

Gate 3N proposes moving code-layer delta production from Python into the native Rust
commit, so the code graph updates *inside* the write lock rather than after it.
We built Gates 5–8 without it. Should we implement it now?

**Answer: Yes, Gate 3N is the correct architecture. The current codebase has a
structural SPEC violation that Gate 3N was explicitly designed to close. It should
be implemented before shipping.**

---

## 1. What Gate 3N fixes (the SPEC violation)

REFINED_SPEC.md §3 defines the write transaction:

```
acquire write lock
  1. mutate content store           → new Generation
  2. apply change to head analysis  → incremental engine update
  3. reconcile identity (§5)        → durable-id map for new revision
  4. update code layer (§6)         → graph delta applied       ← GATE 3N
  5. update other derived layers    → mark stale
  6. compute Delta (§4)             → id-level created/changed/deleted
  7. publish generation + revision  → readers may observe R
release write lock
→ enqueue Delta to subscription bus (§12), outside the lock
```

The critical invariants:

> **[INV] 3.3.1** Steps 1–7 MUST execute under a single mutual-exclusion boundary.
> No layer state MUST be observable in a partially-updated form. In particular,
> layer mutations MUST NOT occur outside the lock that serialised the content
> mutation.

> **[INV] 3.3.3** Publication (step 7) MUST be the last in-lock step, so a reader
> that observes revision R also observes the fully-updated layers for R.

### What the current code actually does

```
Rust (under Mutex):     steps 1, 2, 3, 6, 7    ← lock released here
Python (no lock):       step 4 (update code graph)
                        step 5 (invalidate derived artifacts)
```

The code graph is updated in `TyO3Session._apply_graph_delta()`, which runs *after*
`commit_head()` returns and the Rust `Mutex` is released. Steps 4 and 5 happen
outside the serialisation boundary. This is exactly the failure mode the SPEC
explicitly calls out:

> **Layer mutation outside the lock.** Updating a code graph or derived layer after
> releasing the write lock allows two writers' layer updates to interleave or apply
> out of revision order. This MUST NOT happen (3.3.1).

### Practical impact today

In single-threaded usage this is benign — there's no concurrent writer to interleave
with. But:

1. **Partitioned writers can interleave graph updates.** Two threads each doing
   `edit()` on different files: Thread A commits revision 3, Thread B commits
   revision 4. But Thread B's `_apply_graph_delta` could run before Thread A's,
   applying revision 4's graph delta to a graph that hasn't seen revision 3 yet.
   The revision gate (`g.revision + 1` check) catches this and forces a rebuild,
   but the rebuild reads through the session's *current* head, which is now at
   revision 4 — so revision 3's graph state is silently dropped.

2. **Snapshot inconsistency window.** A snapshot taken at revision R between
   `commit_head` returning and `_apply_graph_delta` completing will observe content
   at R but graph at R−1. This violates §3.3.3.

3. **Future multi-process.** If the substrate is ever behind a server with multiple
   writer processes, the gap becomes a real correctness hazard.

**Gate 3N closes all of these by moving step 4 (the code-layer delta production)
inside the native `Mutex`, as step 4 of the SPEC transaction.** The Python side
becomes a *pure applier* — it receives a pre-computed `CodeDelta` and applies it
deterministically. No FFI calls, no read-surface reads, no identity lookups during
application. Just mutate the rustworkx structure from the DTO.

---

## 2. The architectural shift

### Current architecture

```
┌─────────────────────────────────────────────────────┐
│ Rust (under Mutex)                                   │
│   commit_head()                                      │
│     - mutate content store                           │
│     - apply to engine                                │
│     - reconcile identity                             │
│     - return SyncResult (file-level paths + ids)     │
│     - publish                                        │
│   → lock released                                    │
├─────────────────────────────────────────────────────┤
│ Python                                               │
│   _apply_graph_delta(result)                         │
│     - source = self (TyO3Session, live head)         │
│     - call session.document_symbols(file)    ← FFI   │
│     - call session.file_occurrences(file)    ← FFI   │
│     - call session.class_supertypes(file)    ← FFI   │
│     - call session.check()                  ← FFI   │
│     - look up ids via session.id_for()      ← FFI   │
│     - materialize nodes, resolve edges               │
│     - update rustworkx graph                         │
│   _invalidate_derived(result)                        │
│     - walk derivation DAG, mark stale                │
└─────────────────────────────────────────────────────┘
```

Every graph update makes **O(files × entities)** FFI calls through the GIL. The
cold build is especially expensive: `document_symbols` per file, then
`file_occurrences` per file, then `check()` once, plus `class_supertypes` lookups
during inheritance passes. For a project with 1,000 files and 10,000 symbols, that's
thousands of Python↔Rust round-trips.

### Gate 3N architecture

```
┌─────────────────────────────────────────────────────┐
│ Rust (under Mutex)                                   │
│   commit_head()                                      │
│     - mutate content store                           │
│     - apply to engine                                │
│     - reconcile identity                             │
│     - produce_code_delta(head, dirty_set)    ← NEW   │
│         • extract entities for dirty files           │
│         • materialize nodes (DurableId-keyed)        │
│         • resolve containment edges                  │
│         • resolve reference/import edges             │
│         • resolve inheritance (two-pass)             │
│         • maintain reverse-dep index                 │
│         • diff against prior CodeLayer               │
│         • emit CodeDelta (nodes + edges DTO)         │
│     - return SyncResult (with code_delta field)      │
│     - publish                                        │
│   → lock released                                    │
├─────────────────────────────────────────────────────┤
│ Python                                               │
│   _apply_graph_delta(result)                         │
│     - revision gate check                            │
│     - graph.apply_code_delta(result.code_delta)      │
│         • upsert nodes from DTO (pure)               │
│         • add/remove edges from DTO (pure)           │
│         • update indices (pure)                      │
│     → ZERO FFI calls, ZERO read-surface reads        │
│   _invalidate_derived(result)                        │
│     - walk derivation DAG, mark stale                │
└─────────────────────────────────────────────────────┘
```

The Python applier is a **pure function** of `(self, CodeDelta)`. It touches no
session, no snapshot, no read surface. The `CodeDelta` DTO is the complete
structural description of what changed.

---

## 3. Concrete benefits

### Correctness (the main one)
- **SPEC §3.3.1 compliant.** The code layer updates inside the lock that serialised
  the content mutation. No interleaving possible.
- **SPEC §3.3.3 compliant.** Publication is truly the last in-lock step. Any reader
  observing revision R sees the code graph at R.
- **No Python write lock needed.** The interim `threading.RLock` the guide mentions
  as a prerequisite was never added, and Gate 3N makes it unnecessary anyway —
  ordering is guaranteed by the native Mutex + revision-gated application.
- **`_prime_identity_registry` can be deleted.** Currently `CodeGraph.build()` must
  call `sync_all()` before reading symbols so the identity registry is populated.
  With native delta production, the registry is reconciled *during* the commit,
  and the delta already carries `DurableId`s. No priming pass needed for cold
  builds or snapshots.

### Performance
- **Eliminates O(files × entities) FFI calls per write.** The native producer calls
  the engine directly (no GIL crossing) for occurrences, supertypes, and symbol
  extraction. For a single-file edit, this is ~3–5 FFI calls saved per dirty file
  in the incremental path, and ~3 FFI calls saved per *every* file in a cold build.
  For a 1,000-file project, a cold build currently makes ~3,000+ FFI round-trips
  through Python. With Gate 3N: zero.
- **Salsa memoisation is reused directly.** The native engine already memoises
  unchanged files. The current Python path can't benefit from this because it
  re-extracts through the PyO3 boundary, which doesn't cache. The native producer
  calls the engine inside the same process, so salsa cache hits are immediate.
- **Bounded incremental deltas.** The Gate 3N `CodeLayer` maintains authoritative
  structural state across commits, so the producer diffs against the prior state
  and emits only what changed — not a full rebuild. The current Python
  `apply_delta` re-extracts every dirty file from the read surface.

### Architectural clarity
- **Single authority for structural state.** The `CodeLayer` in `HeadState` is the
  one source of truth for node/edge structure. The Python `CodeGraph` is explicitly
  a replica. Currently both sides derive the same state independently, and parity
  tests keep them aligned — this is fragile.
- **Natural input to Gate 8's subscription bus.** Gate 8 currently reconstructs
  the transitive affected set from the graph's `_file_importers` reverse-dep index
  — an index maintained by the Python builder. With Gate 3N, the native
  `CodeLayer.reverse_deps` index is the authority, and the bus can consume the
  `CodeDelta` directly. The SPEC says the bus "consumes the existing CodeDelta +
  reverse-dep, not recomputes them."
- **Snapshot code layer from native state.** Gate 3N Step 7 has snapshots build
  their code graph from a native `CodeDelta` computed over the snapshot's frozen
  database, rather than walking the Python read surface. This is stronger
  cross-layer consistency (§10.2.2).

### Deletes substantial dead-weight code
- **~1,200–1,500 lines** of Python graph construction code become dead and can be
  removed from `graph.py`: `_collect_symbols_for_file`, `_materialize_file_nodes`,
  `_resolve_references_via_occurrences`, `_inherits_pass_I`, `_overrides_pass_II`,
  `rebuild`, the old `apply_delta` internals, and the `build` method's pass
  orchestration.

---

## 4. What needs to change (implementation scope)

### New Rust code (~500 lines)

| File | What | Status |
|------|------|--------|
| `rust/src/dto/code_delta.rs` | `SymbolNodeDto`, `EdgeDto`, `CodeDelta` wire types | Exists in gate3n-reference, needs review |
| `rust/src/code_layer.rs` | `CodeLayer` data model + `NodeData`/`Edge` + delta assembly | Exists in gate3n-reference, needs rewrite |
| `rust/src/project.rs` | `produce_code_delta()` inside `commit_head()` | New function |
| `rust/src/entity.rs` | Restore `file`, `range`, `selection_range`, `qualified_name` fields | Was reverted, needs re-adding |

### Modified Rust code

| File | Change |
|------|--------|
| `rust/src/project.rs` | `HeadState` gets `code_layer: CodeLayer` field (alongside `authored`) |
| `rust/src/project.rs` | `commit_head()` calls `produce_code_delta()` after reconciliation, attaches to `SyncResultDto` |
| `rust/src/dto/sync.rs` | `SyncResultDto` gets `code_delta: Option<CodeDelta>` field |
| `rust/src/dto/mod.rs` | Add `mod code_delta; pub use code_delta::*;` |
| `rust/src/lib.rs` | Add `mod code_layer;` |
| `rust/src/entity.rs` | Add `file`, `range`, `selection_range`, `qualified_name` fields to `Entity` |

### New Python code (~230 lines)

| File | What |
|------|------|
| `src/tyo3/graph/graph.py` | `apply_code_delta()` — pure applier method |
| `src/tyo3/models/analysis.py` | `CodeDelta`, `SymbolNodeDelta`, `EdgeDelta` Pydantic mirrors |
| `src/tyo3/graph/tests/test_code_delta_apply.py` | Pure applier unit tests |
| `src/tyo3/graph/tests/test_native_code_delta.py` | Integration parity tests |

### Modified Python code

| File | Change |
|------|--------|
| `src/tyo3/session.py` | `_apply_graph_delta()` cut over to `graph.apply_code_delta(result.code_delta)` |
| `src/tyo3/session.py` | Delete `_prime_identity_registry` + call in `snapshot()` |
| `src/tyo3/graph/graph.py` | Delete ~1,200 lines of read-surface build code (Step 7) |

### Conflict resolution required

The `gate1-content-store` branch implemented Gate 3N on top of `gate4-complete`.
The current HEAD has since added Gates 5–8, which modified many of the same files.
Key conflicts:

| File | Gate 3N branch change | HEAD change | Resolution |
|------|-----------------------|-------------|------------|
| `entity.rs` | Added 4 fields for code-layer production | Removed root filtering; added nothing structural | Re-add the 4 fields; HEAD's other changes are compatible |
| `project.rs` | Added `code_layer` to `HeadState`, `produce_full_code_delta()` | Added `authored`, `hash_policies`, `default_hash_profile`, `AuthoredStore` | Both fields coexist in `HeadState` — no conflict |
| `dto/mod.rs` | Added `mod code_delta; pub use code_delta::*;` | Added `AuthoredValueDto`, `AuthoredVersionDto` | Both can coexist |
| `dto/sync.rs` | Added `code_delta: Option<CodeDelta>` field | Added `authored` field | Both can coexist |
| `lib.rs` | Added `mod code_layer;` | No conflicting change | Straightforward |

The conflicts are additive — both branches added independent fields to shared
structs. No design collision. The only removed code is the `path_is_under_root`
filter in `entity.rs`, which HEAD already removed.

**The gate3n-reference code should not be used as-is.** It was never compiled and
targets an older `Entity` shape. The guide's step-by-step plan should be followed,
rewriting the producer against the current codebase.

---

## 5. Risk assessment

### Real risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Reference target resolution parity | **High** | The native producer's `resolve_target` must exactly match the Python builder's target resolution. The implementation report flags `target_qualified_name` format mismatches (dotted vs `::`-joined). Gate 3N guide's parity-oracle pattern (emit delta alongside old builder, assert equality) catches every divergence before cutover. |
| `qualified_name` format mismatch | **High** | Native engine uses dotted form (`User.save`); identity registry uses `::`-joined with absolute-file prefix. Producer must emit the canonical form the replica expects. |
| Inheritance cursor position | **Medium** | `class_supertypes` needs the cursor on the class *name*, not the `class` keyword. The gate3n-reference code uses `range.start` which may point at a decorator. Fix: carry `selection_range` on Entity and query at that position. |
| Performance of full-build-per-commit | **Medium** | If Step 5 (incremental producer) is deferred, every write re-extracts the whole project. This is functionally correct but O(n) per write. Implement Step 5 for bounded incremental. |
| Compilation of gate3n-reference code | **Medium** | It never compiled. Expect borrow-checker fixes on the producer's closure captures of `head`, `root`, and the `abs_files` double-mut. |

### Non-risks

- **"We already have Gates 5–8 working."** Gate 3N is a hardening of the
  *foundation* (steps 1–7 of the SPEC transaction). Gates 5–8 are higher layers
  that consume the code graph — they don't care whether it was built in Rust or
  Python, as long as the API is identical. Gate 3N is backward-compatible with
  everything Gates 5–8 do.
- **"The current code passes all tests."** True — for single-threaded use. The SPEC
  violation only manifests under concurrent partitioned writers or snapshot timing.
  The tests don't exercise these scenarios. The SPEC explicitly requires
  lock-enclosed layer updates (§3.3.1).
- **"rustworkx stays in Python — this violates that."** Gate 3N explicitly
  preserves this boundary. The `CodeGraph` query/algorithm surface (cycles,
  reachability, topological order, `diff`, etc.) is untouched. Only the *build*
  path moves. The premise is: "rustworkx stays in Python for algorithms; Rust owns
  structural state + delta production."

---

## 6. Sequencing: where does Gate 3N fit?

The SPEC's transaction has steps 1–7. Today we have 1, 2, 3, 6, 7 in Rust, and 4, 5
in Python. Gate 3N moves step 4 to Rust. Step 5 (derived layer invalidation) is
partially structural and partially Python orchestration, and can move incrementally.

The guide's steps map cleanly onto the current codebase:

| Step | What | Effort | Depends on |
|------|------|--------|------------|
| 0 | Wire contract + pure Python applier | Small | Nothing (standalone) |
| 1 | Native `CodeLayer` + node materialisation (full build) | Medium | Step 0 |
| 2 | Containment edges (native) | Small | Step 1 |
| 3 | Reference/import edges + reverse-dep index (native) | Medium | Step 2 |
| 4 | Two-pass inheritance (native) | Medium | Step 3 |
| 5 | Incremental producer + inbound revalidation | Medium | Step 4 |
| 6 | Cut over HEAD graph; revision-gated apply; retire priming | Small | Step 5 |
| 7 | Native snapshot code-layer; retire read-surface builder | Medium | Step 6 |
| 8 | Determinism proof, cleanup, re-validation | Small | Step 7 |

**The guide's parity-oracle pattern is the safety net:** Steps 1–5 emit a native
`CodeDelta` *alongside* the still-live Python read-surface build. Parity tests
compare them. The native delta only becomes authoritative at Step 6. The suite
stays green at every step.

Each step is one validated commit. Total: **8 commits**, probably 2–4 focused
sessions of work, depending on how smoothly the target resolution parity issues
resolve.

---

## 7. Recommendation

**Implement Gate 3N.** It is not cosmetic. It closes a SPEC violation (§3.3.1,
§3.3.3) that the SPEC itself identifies as a failure mode. The guide was written
for gate4-complete and the current HEAD is gate8-complete — all the downstream
gates (5–8) are compatible because they consume the code graph through the same
Python API that Gate 3N preserves byte-for-byte.

The implementation should follow the guide's step-by-step plan, **not** the
gate3n-reference code (which was never compiled and targets an old `Entity` shape).
The parity-oracle pattern guarantees correctness: the native delta is emitted
alongside the old builder, compared under the strengthened comparator, and only
made authoritative after every parity scenario passes. No red suite, no risky
cutover.

Priority relative to other work: **High, but not blocking.** The SPEC violation is
real but its practical impact requires concurrent writers to manifest. For
single-agent single-threaded use, it's a correctness gap on paper. For multi-agent
or server deployments, it's a real hazard. Ship it before anyone puts the library
behind a server or runs concurrent partitioned writers.

### If we decide NOT to implement

The SPEC must be updated. Either:
- Downgrade §3.3.1 and §3.3.3 from MUST to SHOULD, acknowledging the Python
  post-commit graph update as an accepted deviation; or
- Document the revision-gated defensive rebuild as satisfying the invariants (though
  it doesn't — it handles ordering but not snapshot consistency).
