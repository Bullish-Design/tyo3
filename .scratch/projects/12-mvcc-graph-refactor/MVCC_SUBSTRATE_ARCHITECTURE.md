# TyO3 MVCC Substrate Architecture

> Complete revision of `INCREMENTAL_SYNC_IMPLEMENTATION_PLAN.md`.
>
> Target: a **live, reactive, multi-reader semantic + graph substrate** that AI
> agents and a developer mutate in real time. Optimised for the cleanest possible
> architecture and maximal reuse of ty / ruff / rustworkx primitives — *not* for
> implementation ease. Consistency model: **MVCC** (snapshot isolation, Option B).

---

## 0. The one fact that dictates everything

Salsa's `Storage::cancel_others` (vendored `salsa-0.26.2/src/storage.rs:160`) does
this on **every** `&mut db` mutation (which includes `apply_changes`):

```rust
fn cancel_others(&mut self) -> &mut Zalsa {
    self.handle.zalsa_impl.runtime().set_cancellation_flag();   // cancel in-flight queries
    let mut clones = self.handle.coordinate.clones.lock();
    while *clones != 1 {                                        // BLOCK until we are the
        clones = self.handle.coordinate.cvar.wait(clones);     // *only* live handle
    }
    Arc::get_mut(&mut self.handle.zalsa_impl).unwrap()
}
```

A `ProjectDatabase::clone()` shares the same `Arc<Zalsa>` and the same clone
counter (`storage.rs:25-35`). Therefore:

> **A held snapshot that is a clone of the live db does not merely risk
> cancellation — it blocks the writer forever.** `apply_changes` cannot proceed
> while any clone is alive.

Consequences, which are not negotiable:

- **Shared-storage MVCC is impossible.** Memo reuse across an edit requires the
  same `Zalsa`; the same `Zalsa` means writes block on readers. The two cannot
  coexist on one storage.
- **Each pinned revision must be its own `Zalsa`** (its own independent
  `ProjectDatabase`), constructed over an immutable view of content. Then the
  writer's `cancel_others` never sees the reader's clone count, never blocks, and
  the reader is never cancelled.
- This is *more* than ty_server does. ty_server keeps one db and tolerates
  cancellation (`server/api.rs:354`, `RETRY_ON_CANCELLATION`). It has one logical
  client. We have many concurrent agent readers and want true isolation, so we
  pay for independent per-revision storage deliberately.

Everything below is the cleanest architecture consistent with that constraint.

---

## 1. Core model: one immutable content store, two database tracks

The whole system is built from a single idea reused everywhere:

> **A database is `ProjectDatabase` over a `System`. The only variable is whether
> that `System` is the mutable head overlay or a frozen, revision-pinned view.**

```
                         ┌─────────────────────────────────────────┐
                         │   ContentStore (immutable, revisioned)   │
   disk reads ─────────► │   Revision(u64) ─► Arc<PersistentMap<    │
   disk-change events ─► │       SystemPathBuf, Arc<Document>>>     │
   agent/editor edits ─► │   append-only; O(1) snapshot via Arc     │
                         └───────────────┬─────────────────────────┘
                                         │ frozen view @ R
        ┌────────────────────────────────┴───────────────────────────────┐
        ▼                                                                  ▼
┌───────────────────────────┐                         ┌─────────────────────────────────┐
│ HEAD track (1, mutable)    │   publish revision R    │ REVISION track (N, immutable)   │
│ ProjectDatabase            │ ──────────────────────► │ Snapshot@R: ProjectDatabase     │
│  over OverlaySystem        │                         │  over FrozenSystem@R            │
│ warm, single-writer        │                         │  own Zalsa → never cancelled    │
│ apply_changes (incremental)│                         │  never blocks the writer        │
│ source of change deltas    │                         │  the MVCC read surface          │
└───────────────────────────┘                         └─────────────────────────────────┘
        │                                                                  │
        ▼ feeds                                                            ▼ feeds
┌───────────────────────────┐                         ┌─────────────────────────────────┐
│ HEAD graph (live, mutable) │   delta {c/u/d files}   │ graph@R (immutable copy-on-pin) │
│ rustworkx PyDiGraph        │ ──────────────────────► │ rustworkx PyDiGraph (.copy())   │
└───────────────────────────┘                         └─────────────────────────────────┘
```

### Why two tracks instead of one

The warm/cold tension is *inherent* (memo reuse ⟺ shared Zalsa ⟺ blocking writes).
We resolve it honestly by serving the two real access patterns separately:

| Track | Storage | Consistency | Speed | Used for |
|---|---|---|---|---|
| **HEAD** | shared, in-place mutated | floats to latest; cancellable | warm | the writer's own work; graph-delta computation; optional "glance at latest" fast path |
| **REVISION** (Snapshot) | independent per revision | pinned to R; never cancelled | cold, warms lazily per query | the agent-facing MVCC read API |

This is not a compromise forced by difficulty — given §0 it is the *only*
coherent shape. We lean into it and make it the explicit public model.

---

## 2. The ContentStore + OverlaySystem (the one component we own)

This is the single substantial piece of Rust we write. It mirrors ty_server's
`LSPSystem` (`ty_server/src/system.rs:161`) and subsumes **three** roles at once,
which is where the elegance compounds:

1. **Unsaved agent/editor buffers** — analyse hypothetical edits without touching
   disk (`SystemVirtualPath` + system-path overlays; the Option-6 capability).
2. **The frozen content source for MVCC snapshots.**
3. **A read-once, write-through immutable cache over `OsSystem`.**

### 2.1 Content as an immutable, revisioned, structurally-shared map

```rust
type Generation = Arc<rpds::HashTrieMap<SystemPathBuf, Arc<Document>>>;

struct ContentStore {
    head: Generation,           // current content; persistent map ⇒ O(log n) insert, O(1) clone
    revision: Revision,         // application-level monotonic counter
}

enum Document {
    Text(Arc<str>),             // overlay/agent buffer OR cached disk read
    Notebook(Arc<Notebook>),    // reuse ruff_notebook
    Deleted,                    // tombstone (a path known-absent at this revision)
}
```

Use a persistent/HAMT map (`rpds` or `im`) so that **publishing a revision is
`Arc::clone` of the generation — O(1)** and structurally shared. A snapshot owns a
`Generation`; the writer advances `head` to a new generation. No copying.

### 2.2 OverlaySystem (HEAD) and FrozenSystem (REVISION) are the same impl

```rust
struct OverlaySystem {
    content: ArcSwap<Generation>,   // live head generation, lock-free reads
    native: OsSystem,               // disk fallback for not-yet-captured paths
    frozen: Option<Revision>,       // None = head (live); Some(R) = pinned, no live fallthrough
}

impl System for OverlaySystem {
    fn read_to_string(&self, path) -> Result<String> {
        match self.content.load().get(path) {
            Some(Document::Text(s)) => Ok(s.to_string()),
            Some(Document::Deleted) => Err(not_found(path)),
            None if self.frozen.is_none() => self.native.read_to_string(path), // read-once
            None => Err(not_found(path)), // frozen: revision never read disk for this path
        }
    }
    // path_metadata / source_type / read_virtual_path_to_string / walk_directory ...
    // all: overlay-first, native-fallback — copied structurally from LSPSystem.
}
```

**The MVCC correctness invariant**: a file's content for revision R is fixed the
moment it is *first interned* into the store at R. Disk is read at most once per
(path, revision); the result is captured into the store as an immutable
`Document`. The substrate is the **system of record for in-flight content** — all
mutation (agent edits, editor buffers, *and* developer disk saves surfaced via the
watcher) flows through the store as explicit revision-producing events. A frozen
snapshot therefore never races live disk (this is precisely the bug the current
eager-materialisation `snapshot()` works around by hand — here it is structural).

> We reject the alternative "capture every project file eagerly at publish"
> (today's `project.rs:787` loop): O(files) per snapshot, and it still can't
> represent virtual buffers. The read-once store gives O(1) publish *and* unifies
> disk + overlay + virtual.

### 2.3 Why not fork salsa storage to get warm isolated snapshots?

Considered and rejected: salsa exposes no deep-fork-with-memos
(`StorageHandle::clone` shares; `into_zalsa_handle` just discards thread-local
state). Building one means forking salsa — the opposite of a thin library. Cold
per-revision memos are the accepted, inherent price of true isolation.

---

## 3. Revisions, writes, and publication

### 3.1 The write path (single writer, serialized)

```rust
struct Head {
    db: ProjectDatabase,        // over OverlaySystem (frozen = None)
    store: ContentStore,
    graph: HeadGraphHandle,     // see §5
}

// All of these are serialized by one Mutex<Head>. Each returns the new Revision.
fn edit(path, text)        // agent/editor buffer edit: store.insert(Text) + ChangeEvent
fn edit_virtual(uri, text) // unsaved/scratch buffer: virtual path overlay
fn sync_path(path)         // disk change ingested: store invalidates path + ChangeEvent
fn save(path) / discard(path) // optional: flush overlay → disk / drop overlay back to disk
```

Each write:

1. Mutates the `ContentStore` head generation (persistent insert).
2. Synthesises the precise `Vec<ChangeEvent>` (we always know which paths moved —
   §4) and calls `head.db.apply_changes(&events, None)` → `ChangeResult`
   (`ty_project/src/db/changes.rs:18`). This does **all** the hard incremental
   work: file/dir/config/ignore/stdlib sync, project rediscovery, file-set
   walking. We reimplement none of it.
3. Bumps the application `Revision`.
4. Computes the **file-level delta** and applies it to the HEAD graph (§5).
5. Publishes: `Arc::clone` the new generation; record it as the head revision.

Because the HEAD db is shared/mutable, step 2's `apply_changes` is warm and
incremental. Because no agent snapshot is a *clone of the HEAD db*, `cancel_others`
sees `clones == 1` and never blocks (§0).

### 3.2 The change delta (the bridge to the graph)

`ChangeResult` only reports `project_changed` / `custom_stdlib_changed`
(`changes.rs:25-31`) — not which files. That's fine: **we synthesise the events,
so we already know the delta.** We carry it ourselves:

```rust
struct SyncDelta {
    revision: Revision,
    created: Vec<File>,
    changed: Vec<File>,
    deleted: Vec<File>,
    project_changed: bool,
    custom_stdlib_changed: bool,
    rescan: bool,               // Rescan ⇒ delta unknown ⇒ full graph rebuild
}
```

Exposed to Python as `SyncResult`. This is the first-class object that makes the
graph an incremental layer rather than an afterthought.

### 3.3 Snapshots (the MVCC read surface)

```rust
fn snapshot(at: Option<Revision>) -> Snapshot
```

- Captures the `Generation` for revision R (`Arc::clone`, O(1)) and builds a
  **fresh** `ProjectDatabase` over `FrozenSystem@R` (`frozen = Some(R)`).
- That db has its own `Zalsa`. It can never be cancelled by HEAD writes and never
  blocks them. Memos warm lazily, per query, only for the slice the reader
  touches.
- Snapshot lifetime is explicit (`close()` / context manager / `Drop`). Holding
  the `Generation` alive is what pins the content; dropping it lets the store GC
  superseded generations.
- `at=None` ⇒ pin current head. `at=R` ⇒ time-travel to any still-retained
  revision (agents can diff "before/after my edit").

---

## 4. Sync path resolution & event synthesis (small, delegated core)

Keep the read-path resolver (`rust/src/files.rs`, canonicalising) and add a
**separate** sync resolver — the original plan's instinct was right, but here it
also feeds the store:

- Absolute → as-is; relative → join root; never canonicalise the leaf (deletes and
  not-yet-created paths must be representable).
- Classify with ty's own `ExistingPathKind::from_system` and
  `db.files().try_system` to choose `Created{File|Directory}` /
  `Changed{FileContent}` / `Deleted{Any}` — reuse ty's vocabulary verbatim
  (`ty_project::watch::ChangeEvent`).
- Virtual buffers map to `CreatedVirtual` / `ChangedVirtual` / `DeletedVirtual`.
- `sync_all` / `reload` ⇒ `[ChangeEvent::Rescan]` ⇒ `rescan: true` ⇒ graph full
  rebuild (we cannot derive a delta from a rescan).

File watching is **not** a separate subsystem: ty's `ProjectWatcher` /
`directory_watcher` becomes just another *change source* feeding `sync_path`/the
store via `poll_changes()`. Manual `edit()`/`sync_path()` remain the deterministic
test baseline.

---

## 5. The graph as a first-class incremental MVCC layer

The graph mirrors the db's two-track model exactly (symmetry = elegance), and
delegates every algorithm to rustworkx.

### 5.1 HEAD graph: incremental update from `SyncDelta`

Today `CodeGraph.rebuild(session, path)` (`graph.py:1373`) ignores `path` and full-
rebuilds. Replace with a delta-driven update using the ownership index that
already exists (`_file_to_nodes`, `graph.py:1393`):

```python
def apply_delta(self, snapshot: Snapshot, delta: SyncDelta) -> None:
    if delta.rescan:
        self._replace_with(CodeGraph.build(snapshot)); return

    dirty = set(delta.changed) | set(delta.deleted)
    # 1. Drop nodes owned by changed/deleted files.
    #    rustworkx remove_node auto-removes incident edges.
    self._graph.remove_nodes_from(
        [n for f in dirty for n in self._file_to_nodes.get(f, ())]
    )
    # 2. Re-extract nodes + intra/out edges for created+changed files.
    for f in set(delta.created) | set(delta.changed):
        self._index_file(snapshot, f)          # symbols → nodes, references → out-edges
    # 3. Revalidate INBOUND cross-file edges into changed files.
    #    Reverse-dependency index, recomputed via ty resolution.
    for importer in self._importers_of(dirty):
        self._reresolve_out_edges(snapshot, importer, into=dirty)
```

- **Node identity is content-addressed** (existing `identity.py`): a symbol that
  didn't change keeps its node id across revisions. Deltas stay minimal and
  *graph diffing across revisions becomes meaningful* ("what edges did my edit
  add/remove?") — a first-class agent capability.
- **Cross-file edges** are the only subtle part. Maintain `importers_of[file]`
  (reverse of the dependency edges) and re-resolve only those importers' edges
  into the dirty set, using ty's `file_occurrences` / `goto_definition`. No global
  rescan.
- Everything algorithmic — cycles, ancestors/descendants (for reverse-dep
  closure), shortest paths, connected components, centrality — stays on rustworkx
  built-ins. The library orchestrates; it does not implement graph theory.

### 5.2 Pinned graph snapshots: `Snapshot.graph()`

rustworkx has no copy-on-write graph, so:

- **Recommended:** copy-on-pin. `Snapshot.graph()` lazily `PyDiGraph.copy()`s the
  HEAD graph as of revision R (C-level, fast) and caches it on the snapshot. Query
  performance is full-speed; immutability is structural.
- **Purist alternative (documented, not chosen):** an append-only
  interval-tagged graph where each node/edge payload carries `[r_start, r_end)`
  and a snapshot is a revision-filtered view. Theoretically O(1) pins and true
  graph-level MVCC, but every query pays an O(V+E) revision filter — rejected on
  performance grounds. Copy-on-pin gives the same observable semantics without it.

A `Snapshot` thus offers a fully consistent pair: `snapshot.check()` and
`snapshot.graph()` describe the *same* revision R.

---

## 6. Public Python API

```python
session = TyO3Session(root)            # owns HEAD db + ContentStore + HEAD graph

# ── write / mutate (each returns the new Revision) ───────────────────────────
r = session.edit("a.py", new_text)     # agent buffer edit (overlay; no disk write)
r = session.edit_many({...})           # batched, one revision
r = session.edit_virtual("untitled:1", text)   # unsaved/scratch buffer
session.sync_path("a.py")              # ingest a disk change
session.sync_all()                     # rescan (== reload)
session.save("a.py"); session.discard("a.py")  # overlay ↔ disk (optional)
session.watch(); session.poll_changes()        # ty ProjectWatcher as a change source

session.head                           # current Revision

# ── read: MVCC, consistent, never cancelled ──────────────────────────────────
with session.snapshot() as snap:       # pin head; or snapshot(at=r) to time-travel
    snap.check(); snap.document_symbols("a.py"); snap.goto_definition(...)
    g = snap.graph()                   # immutable CodeGraph @ snap.revision
    snap.revision

before = session.snapshot(at=r0)
after  = session.snapshot(at=r1)
diff   = after.graph().diff(before.graph())   # what my edit changed (optional, elegant)

# ── live HEAD graph (updates incrementally as syncs land) ────────────────────
session.graph                          # mutable head graph view

# ── optional convenience: floating "latest" reads (cancellable fast path) ────
session.check()                        # == an internally-retried head read; documented as floating
```

### 6.1 A cleanliness win this unlocks

`rust/src/project.rs` currently **duplicates all ~20 read wrappers** between
`PyTyProject` and `PySnapshot` (lines ~799-1196 vs ~1212-1610). In the new model
there is exactly one read surface — the snapshot/db-view — and `TyO3Session`'s
convenience reads are sugar over `head_snapshot()`. The duplication collapses to a
single implementation. Likewise the bespoke eager-materialisation in `snapshot()`
disappears (replaced by the store), and `reload()`'s full reconstruct becomes
`sync_all`.

---

## 7. Concurrency model (summary)

- **One writer.** A `Mutex<Head>` serialises every mutation. Writes are cheap:
  persistent-map insert (O(log n)) + incremental `apply_changes` (warm) + graph
  delta + O(1) publish.
- **N readers.** Each `Snapshot` owns an independent `ProjectDatabase`. Because no
  snapshot shares the HEAD `Zalsa`, the writer's `cancel_others` always observes
  `clones == 1` on HEAD → **never blocks**; snapshots → **never cancelled**
  (§0). This is the entire payoff of paying for per-revision storage.
- **GIL released** during all analysis (preserve the existing `py.detach`
  pattern). Snapshots are `Send` and independent, so many threads/agents read in
  true parallel.
- The optional `session.check()` floating fast path is the *only* place
  cancellation can occur; it is internally caught and retried (ty_server's
  `RETRY_ON_CANCELLATION` pattern, `server/api.rs:354`) and documented as
  non-pinned.

---

## 8. Thinness inventory — delegate vs own

**Delegated (we write ~none of this):**

| Concern | Delegated to |
|---|---|
| Incremental file/dir/config/ignore/stdlib sync, project rediscovery | `ProjectDatabase::apply_changes` |
| Change vocabulary | `ty_project::watch::ChangeEvent` + `*Kind` enums |
| Content / virtual / overlay semantics | `System` / `WritableSystem` traits; mirror `LSPSystem` |
| Path classification | `ExistingPathKind::from_system`, `db.files().try_system` |
| Config discovery | `ProjectMetadata::discover` + `apply_configuration_files` |
| Memoisation & durability (stdlib = `HIGH`) | salsa (free, per-Zalsa) |
| Cancellation on the floating fast path | `salsa::Cancelled::catch` |
| All semantic analysis | `ty_ide` |
| File watching | `ty_project::watch::ProjectWatcher` / `directory_watcher` |
| Every graph algorithm | `rustworkx` |
| Notebook handling | `ruff_notebook` |

**Owned (minimal, all small except the store):**

- `ContentStore` + `OverlaySystem`/`FrozenSystem` — the one real component;
  structurally a copy of `LSPSystem` + a persistent-map content layer.
- `Revision` / publish / snapshot plumbing.
- Sync-path resolver + `ChangeEvent` synthesis + `SyncDelta`.
- Graph delta application (Python orchestration over rustworkx).

---

## 9. Inherent costs (stated honestly, since correctness ≠ free)

- **Cold snapshot memos.** A fresh snapshot pays warmup for the slice it queries.
  Mitigated by: snapshots are *held and queried many times* (amortised), and the
  floating `session.check()` fast path serves one-off "latest" glances warm.
  Inherent to true isolation (§0/§2.3) — not removable without forking salsa.
- **Memory per live snapshot.** Each snapshot's db retains its own memos. The
  *content* is shared (Arc'd generations); only memos are per-snapshot. Bound it
  by explicit snapshot lifecycle + a retained-revision cap.
- **Reverse-dependency index upkeep** in the graph. The price of avoiding global
  rescans on every edit; small and local.

These are the genuine tradeoffs of the chosen model. They are acceptable for a
substrate whose defining feature is many concurrent, consistent agent readers.

---

## 10. Implementation phases (ordered by architectural dependency, not difficulty)

1. **ContentStore + OverlaySystem/FrozenSystem.** Persistent-map content layer;
   `System` impl mirroring `LSPSystem`; read-once disk capture. The foundation —
   everything else sits on it.
2. **HEAD db over OverlaySystem.** Replace `use_defaults`/`OsSystem` construction
   with discovery (`ProjectMetadata::discover` + `apply_configuration_files` +
   `ProjectDatabase::fallible`) over the overlay. `reload` → `sync_all`.
3. **Revisions + write path.** `edit` / `edit_virtual` / `sync_path` / `sync_all`
   → store mutation + `apply_changes` + `SyncDelta`. Application `Revision`.
4. **Snapshots (MVCC surface).** `snapshot(at)` builds independent db over
   `FrozenSystem@R`. Collapse the duplicated read walls into one read view; make
   `TyO3Session` reads sugar over `head_snapshot()`. Delete eager materialisation.
5. **Concurrency proof.** Stress: N reader snapshots + a hot writer. Assert writer
   never blocks, snapshots never cancelled, results match the pinned revision.
6. **Graph: HEAD incremental.** `apply_delta` from `SyncDelta`; reverse-dep index;
   rustworkx `remove_nodes_from` / re-index / re-resolve. Retire the full-rebuild
   stub.
7. **Graph: pinned snapshots.** `Snapshot.graph()` copy-on-pin; cross-revision
   `graph.diff`.
8. **Watcher as a change source.** `ProjectWatcher` → `poll_changes` → store/sync.
9. **Floating fast path.** Optional `session.check()` etc. with cancel-retry.
10. **Benchmarks.** Per §11.

(2)–(4) deliver the MVCC engine; (6)–(7) deliver the incremental graph. Both are
load-bearing for the product thesis, so neither is "future work."

---

## 11. Verification & benchmarks (`devenv shell -- ...`)

- **Isolation:** snapshot@R pinned; many HEAD edits land; snapshot@R still reads R.
- **Liveness:** writer throughput unaffected by K held snapshots (proves no
  `cancel_others` block).
- **No cancellation:** snapshot reads never surface `salsa::Cancelled`.
- **Parity:** HEAD state after a sequence == fresh rebuild over the same content.
- **Graph delta == rebuild:** `apply_delta` result == `CodeGraph.build` for the
  same revision (changed / created / deleted / cross-file-edge cases).
- **Virtual buffers:** `edit_virtual` analysed without disk writes; never leaks to
  disk-backed snapshots of earlier revisions.
- **Perf:** single-file edit → sync+graph-delta ≪ full rebuild; snapshot warm-up
  cost characterised; memory bounded across many revisions.

Run under the project's devenv scripts (`check-rust`, `rebuild`, `tests`).

---

## 12. What changed from the original plan

| Original plan | This revision |
|---|---|
| Dual mode `rebuild` vs `incremental_disk`; in-place mutation of the live db; cancel-retry on every read | MVCC: independent per-revision storage; writer never blocks, readers never cancelled. `cancel-retry` confined to one optional floating fast path |
| Snapshots disabled/guarded in incremental mode (Phase 6A); independent snapshots a far-future "Phase 6B" | Independent snapshots **are** the read surface from the start; the store makes them O(1) and structurally correct |
| Disk-only `sync_path`; virtual buffers a "stepping stone" (Phase 8) | Overlay store handles disk + overlay + virtual uniformly on day one; agents analyse hypothetical edits natively |
| Graph layer untouched | Graph is a co-equal incremental MVCC layer driven by `SyncDelta`, mirroring the db tracks |
| `InMemorySystem` prototype → `FrozenOverlaySystem` later | One `OverlaySystem`/`FrozenSystem` + persistent ContentStore; no throwaway prototype |
| "Make incremental the default" as an end goal | No ranked default: `snapshot()` for consistency, floating reads for latest, distinct by intent |
