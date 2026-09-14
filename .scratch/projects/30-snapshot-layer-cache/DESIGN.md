# DESIGN — Snapshot code-layer cache (project 30)

Measured on `main` at `314371c8` (post-project-29) on 2026-09-13/14.

---

## 1. The measurements

Project under test: TyO3's own `src/tyo3` — **74 Python files, 3258 nodes,
32849 edges**.

### 1.1 Cost of a first `snapshot.graph()`, decomposed

```
full_code_delta   (Rust Builder, full rebuild)   2.006s   72%   <- redundant
apply_code_delta  (Python, wholesale replace)    0.682s   24%
refresh_diagnostics (ty check)                   0.110s    4%
                                          total  2.798s
```

### 1.2 It repeats on every fresh snapshot

Four successive edits, each followed by a new snapshot:

| Edit | commit | first `snap.graph()` | memoized `snap.graph()` |
|---|---|---|---|
| 0 | 1.700s | 2.090s | 0.0000s |
| 1 | 2.505s | 2.987s | 0.0000s |
| 2 | 2.736s | 2.711s | 0.0000s |
| 3 | 2.888s | 2.507s | 0.0000s |

Memoization works *within* a snapshot (`src/tyo3/session/views.py:286`,
`self._graph`) and does nothing *across* snapshots. Every new revision pays
again.

### 1.3 Standalone timings

```
open                    0.079s
snapshot()              0.004s
full_code_delta (cold)  1.519s
  again, same snapshot  0.519s     (Salsa warm; the Builder still runs in full)
  on a fresh snapshot   1.669s
```

The 0.519s repeat is the floor a warm Salsa gives you. It is not the fix — the
Builder still walks every file.

---

## 2. The mechanism

```
commit
  └─ produce_code_delta_scoped(state, prev, seed_dirty, rev)
       └─ builder.build_scoped(...)          re-derives only the dirty scope
       └─ result: a COMPLETE CodeLayer
            "byte-identical to a full `Builder::build` over the same final
             content"                        rust/src/code_layer.rs:525
  └─ head.code_layer = next                  rust/src/project/commit.rs:948

snapshot
  └─ TyProjectState { db, root, registry, ... }   rust/src/project.rs:81
       ^ no code layer field — this is the gap
  └─ Snapshot::full_code_delta                    rust/src/project/snapshot.rs:58
       └─ produce_code_delta(&state, &EMPTY, rev, true, None)
            └─ builder.build()               FULL rebuild, from nothing
```

The commit produces a complete, verified layer every revision. The snapshot
discards it and rebuilds. That is the whole defect.

### 2.1 Who pays

`src/tyo3/session/views.py:47` — `_RecordingContext.__init__`:

```python
self._graph = snapshot.graph()
```

Unconditional. A traced derived producer that touches only its own entity still
triggers the full rebuild before `produce()` runs. Also paid by
`session.graph()` (`session/session.py:241`), `Snapshot.graph()`
(`views.py:293`) and `CodeLayerView._node_index` (`layers/code.py`), though the
last reads the delta directly without building a `CodeGraph`.

---

## 3. Two candidate designs

### 3.1 Design A — carry an `Arc<CodeLayer>` on the read clone (recommended)

Add to `TyProjectState`:

```rust
pub(crate) code_layer: Option<Arc<CodeLayer>>,
```

`HeadState.code_layer` becomes `Arc<CodeLayer>`; the commit's
`head.code_layer = next` becomes `head.code_layer = Arc::new(next)`.
`ReadCloneSource::read_clone` clones the `Arc` (refcount bump, no deep copy).
`Snapshot::full_code_delta` then diffs the carried layer against an empty prev
instead of rebuilding:

```rust
let layer = state.code_layer.as_deref().unwrap_or(&EMPTY);
let delta = layer.diff_from(&EMPTY, revision, true);
```

- **Pro:** removes the full 2.0s. Snapshots at one revision share one
  allocation. No behaviour change — the carried layer *is* what a rebuild would
  produce.
- **Con:** memory per live revision (§4). The `Option` is needed because a
  read clone may be taken before any commit has produced a layer.

Keep the rebuild path as a fallback when the `Option` is `None`, so nothing can
regress to wrong output — only to slow output.

### 3.2 Design B — lazy rebuild, memoized per revision on the session

Keep `TyProjectState` untouched; add a session-side `dict[revision, CodeGraph]`.

- **Pro:** no Rust change, no per-snapshot memory.
- **Con:** does not fix the native `full_code_delta` path (`CodeLayerView` still
  rebuilds), leaks across revisions unless bounded, and puts a consistency
  question in Python that Rust currently answers by construction. It also
  re-introduces the "graph as a second cache" shape that `Project 31, #1/#2`
  deliberately removed.

**Take Design A.** B trades a real architectural property for a smaller diff.

---

## 4. The memory question — measure this first

The estimate is **~6 MB per revision** for this project:

```
32849 edges × ~165 B   ≈ 5.4 MB
 3258 nodes × ~250 B   ≈ 0.8 MB
```

Per-edge: `source` + `target` Strings (~26 B each of ULID/path text plus 24 B
of `String` overhead), an `EdgeKind` discriminant, and three `Option`s
(`role`, `file`, `range`).

**This is arithmetic, not a measurement.** Before implementing:

1. Measure the real resident size of one `CodeLayer` at this scale.
2. Decide the retention bound. Today a pinned snapshot holds a `ProjectDatabase`
   clone already, so the layer is not the only per-snapshot cost — establish
   what fraction it adds.
3. Decide whether to bound the number of live pinned revisions, or let the
   existing snapshot lifecycle bound it.

If the real figure lands far above the estimate, revisit Design B.

### 4.1 Step 0 measurement (2026-09-14)

A temporary Rust test used a counting allocator and cloned each retained
component independently, so builder scratch and Salsa allocations were not
mistaken for layer memory. The current checkout's repository-root session is
the closest match to the documented 3,258-node / 32,849-edge scale; it now
contains 174 source paths, 3,406 nodes, and 36,328 edges. The measured live
allocations were:

| component | bytes | MiB |
|---|---:|---:|
| nodes | 1,617,346 | 1.54 |
| edges | 10,247,032 | 9.77 |
| reverse_deps | 1,427,865 | 1.36 |
| file_to_nodes | 166,460 | 0.16 |
| `CodeLayer` inline map fields | 96 | — |
| **retained layer total** | **13,458,799** | **12.83** |

The same probe measured the existing pinned `build_frozen` state (including
its independent `ProjectDatabase`) at 1,079,359 bytes / 1.03 MiB. A package-
only root (`src/tyo3`) measured 75 source paths, 1,781 nodes, 16,052 edges,
and 5,877,757 bytes / 5.61 MiB for the layer. The process RSS delta during
the full build was much larger (about 197 MiB at repository scale) because it
also retained analysis/Salsa working data; it is not the per-layer figure.

The measured repository-scale layer is about 2.1× the arithmetic estimate,
but Design A remains the only candidate that fixes both snapshot producer
paths. We therefore retain the existing snapshot lifecycle as the bound: no
new live-snapshot cap is introduced by this project. The larger measured
share is recorded as an explicit follow-up risk for any future snapshot
retention policy.

---

## 5. What this does not address

`apply_code_delta` (0.682s, 24%) stays. The Python applier is a deliberate
*wholesale replacement* — "The incremental machinery … was retired with the
per-commit incremental path" (`src/tyo3/graph/applier.py:88`, Project 31 #1).
Making it incremental again would reinstate exactly what that project removed.
Leave it. If it becomes the dominant cost after this project, size it separately
and argue it on its own merits.

`refresh_diagnostics` (0.110s, 4%) is a real type check. Not redundant.
