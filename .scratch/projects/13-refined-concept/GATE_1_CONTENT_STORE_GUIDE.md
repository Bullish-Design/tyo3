# Gate 1 — Authoritative Per-Revision Content Store: Implementation Guide

> Implements **REFINED_SPEC.md §1** (and the parts of §2 and §6.1 that depend on
> it). This is a foundational **gate**: until every requirement here holds,
> snapshot isolation, the delta, identity reconciliation, and all derived layers
> are untrustworthy. Do not start Gate 2 until Gate 1's final acceptance gate
> passes.
>
> **Audience:** an engineer new to the codebase. Every step states the goal, the
> files to touch, what to build, and a validation you must pass *before* moving
> on. Do not skip a validation. If a validation fails, fix it before proceeding —
> a broken foundation compounds.

## How to work in this repo

- All commands run inside the project's dev shell. Prefix everything:
  `devenv shell -- <script>`.
- Rust unit tests: `devenv shell -- test-rust`
  (equivalently `devenv shell -- cargo test --manifest-path rust/Cargo.toml`).
- Full suite (Rust + Python): `devenv shell -- tests`.
- Rebuild the native module after Rust changes: `devenv shell -- build`.
- Never run `pytest`/`cargo` bare; always through `devenv shell --`.
- Work on a branch. Commit after each step's validation passes, so a failed step
  is one `git reset` away. Use small, labelled commits (`gate1: step 3 — ...`).

## The contract you are building toward (read first)

The single invariant that defines success (SPEC §1.3.1):

> For a fixed revision R, the content returned for R is identical no matter when
> or by whom it is read. Disk is never consulted to satisfy a read at a committed
> revision.

Everything in this guide exists to make that true, including for directory
membership (§1.3.3) and for disk changes ingested by sync/watch (§1.3.2).

## Target module layout

You will end with these Rust modules (create them if absent, evolve them if
present):

```
rust/src/
  hash.rs        # ContentHash + hashing entry points (Step 1)
  content.rs     # Document, ContentMap, Generation, ContentStore (Steps 2–4, 9)
  overlay.rs     # OverlaySystem (live + frozen) implementing ty's System (Steps 5–7)
  project.rs     # commit transaction wiring, snapshot capture (Steps 8–10)
```

Keep each type's `// CONCURRENCY:` and `// INVARIANT:` comments accurate — they
are part of the deliverable.

---

## Step 1 — `ContentHash` and the hashing entry point

**Goal.** A 128-bit content hash type, computed once and carried on content. Full
AST-normalised hashing is Gate 2's concern (SPEC §7); here you build the *type* and
a byte-level hashing function that Gate 2 will later refine. This keeps Gate 1
self-contained and unblocks Step 2.

**Files.** `rust/src/hash.rs` (new); register `mod hash;` in `rust/src/lib.rs`.

**Build.**
- Define `ContentHash(pub u128)` deriving `Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord`.
- Add `pub fn hash_bytes(bytes: &[u8]) -> ContentHash` using a fast, stable
  128-bit hash (e.g. `xxhash-rust` xxh3_128, or `blake3` truncated to 128 bits).
  Pick one dependency, add it to `rust/Cargo.toml`, and document the choice in a
  module comment.
- Add `pub fn hash_text(text: &str) -> ContentHash { hash_bytes(text.as_bytes()) }`.
- Add a `// NOTE:` comment: "Gate 2 (SPEC §7) replaces text hashing with
  AST-normalised hashing for entity identity; this byte hash is the content-store
  default and remains correct for whole-file content."

**Validate.**
- `devenv shell -- test-rust` compiles.
- Add unit tests in `hash.rs`:
  - `hash_text("a") == hash_text("a")` (determinism).
  - `hash_text("a") != hash_text("b")` (distinctness).
  - hashing is stable across two process runs (hard-code one known value:
    `assert_eq!(hash_text("X = 1\n").0, <printed-value>);` — run once, paste the
    value, lock it in).
- **Acceptance gate:** all three tests pass. Commit.

---

## Step 2 — `Document` carries its hash

**Goal.** Make every text document carry its `ContentHash` and a monotonic
`version`, and represent deletion as a tombstone (SPEC §1.2, §1.3.7).

**Files.** `rust/src/content.rs`.

**Build.**
```rust
use crate::hash::{ContentHash, hash_text};

#[derive(Debug, Clone)]
pub enum Document {
    Text { text: Arc<str>, hash: ContentHash, version: u64 },
    Deleted { version: u64 },
}

impl Document {
    pub fn version(&self) -> u64 { /* match both arms */ }
    pub fn text(text: impl Into<Arc<str>>, version: u64) -> Self {
        let text = text.into();
        let hash = hash_text(&text);
        Document::Text { text, hash, version }
    }
    pub fn hash(&self) -> Option<ContentHash> {
        match self { Document::Text { hash, .. } => Some(*hash), _ => None }
    }
}
```

**Validate.**
- Unit test: `Document::text("Y = 2\n", 1).hash() == Some(hash_text("Y = 2\n"))`.
- Unit test: `Document::Deleted { version: 1 }.hash() == None`.
- **Acceptance gate:** `devenv shell -- test-rust` green. Commit.

---

## Step 3 — `ContentMap` and `Generation`

**Goal.** An immutable, structurally-shared map of all content at one revision,
covering both system paths and virtual paths, with O(1) clone (SPEC §1.2, §1.3.4).

**Files.** `rust/src/content.rs`.

**Build.**
```rust
use rpds::HashTrieMapSync;
use ruff_db::system::{SystemPathBuf, SystemVirtualPathBuf};

#[derive(Debug, Clone)]
pub struct ContentMap {
    pub system:  HashTrieMapSync<SystemPathBuf, Document>,
    pub virtual_files: HashTrieMapSync<SystemVirtualPathBuf, Document>,
}

impl ContentMap {
    pub fn new() -> Self { /* both maps empty */ }
}

pub type Generation = Arc<ContentMap>;
```
- Add `mod doc` comments noting: cloning a `ContentMap` is two `Arc` bumps and
  structurally shares with the prior generation; capturing a `Generation` is one
  `Arc` clone.

**Validate.**
- Unit test: insert into a clone; assert the original is unchanged (persistence).
- Unit test: `Arc::ptr_eq` after `Arc::clone(&gen)` confirms O(1) capture shares
  the allocation.
- **Acceptance gate:** `devenv shell -- test-rust` green. Commit.

---

## Step 4 — `ContentStore` with single-revision batch mutation

**Goal.** The mutable head-side owner of content. Crucially, **a batch of changes
produces exactly one revision and one retained generation** (SPEC §1.2, §6.1.1).

**Files.** `rust/src/content.rs`.

**Build.**
```rust
use std::collections::BTreeMap;

pub struct ContentStore {
    generation: Generation,
    revision: Revision,
    version_counter: u64,
    retained: BTreeMap<Revision, Generation>,
    retain_cap: usize,
}
const DEFAULT_RETAIN_CAP: usize = 256;
```
- `new()` seeds `Revision(0)` with an empty generation, recorded in `retained`.
- `capture(&self) -> Generation` → `Arc::clone(&self.generation)`.
- `generation_at(&self, r) -> Option<Generation>` → `retained.get(&r).cloned()`.
- `revision(&self)`, `oldest_retained(&self)`, `has_overlay(&self, path)`.
- **Single private `mutate`** applies a closure to a clone of the map, swaps the
  generation, bumps the revision **once**, and records the retained generation:
```rust
fn mutate(&mut self, f: impl FnOnce(&mut ContentMap, &mut dyn FnMut() -> u64)) -> Revision {
    let mut map = (*self.generation).clone();
    let mut next_version = || { self.version_counter += 1; self.version_counter };
    f(&mut map, &mut next_version);
    self.generation = Arc::new(map);
    self.revision = Revision(self.revision.0 + 1);
    self.record_retained();
    self.revision
}
```
- **Public batch API** — the key requirement. Every write entry is one of:
```rust
pub enum Change {
    Insert { path: SystemPathBuf, text: Arc<str> },
    Delete { path: SystemPathBuf },
    InsertVirtual { path: SystemVirtualPathBuf, text: Arc<str> },
    ForgetVirtual { path: SystemVirtualPathBuf },
    Forget { path: SystemPathBuf },   // drop overlay → fall through (live head only)
}

pub fn apply_batch(&mut self, changes: Vec<Change>) -> Revision {
    self.mutate(|m, next_version| {
        for c in changes {
            match c {
                Change::Insert { path, text } => {
                    let d = Document::text(text, next_version());
                    m.system = m.system.insert(path, d);
                }
                Change::Delete { path } => {
                    m.system = m.system.insert(path, Document::Deleted { version: next_version() });
                }
                Change::Forget { path } => { m.system = m.system.remove(&path); }
                Change::InsertVirtual { path, text } => {
                    let d = Document::text(text, next_version());
                    m.virtual_files = m.virtual_files.insert(path, d);
                }
                Change::ForgetVirtual { path } => { m.virtual_files = m.virtual_files.remove(&path); }
            }
        }
    })
}
```
- Keep `record_retained` evicting oldest beyond `retain_cap`.
- Provide thin helpers `insert_text`, `delete`, `insert_virtual` that call
  `apply_batch(vec![..])` with a single change (one revision each), for ergonomic
  single edits.

> **Why this shape:** a per-change `mutate` loop would bump the revision per change
> and retain intermediate half-batch generations — directly violating §6.1.1.
> Routing *all* multi-change writes through `apply_batch` guarantees one revision.

**Validate.**
- Unit test (atomicity): `apply_batch(vec![Insert a, Insert b])` returns `R0+1`
  (not `R0+2`); `generation_at(R0+1)` contains both `a` and `b`;
  `generation_at(R0)` contains neither; **there is no generation between R0 and
  R0+1** (assert `generation_at(Revision(R0.0+1))` is the only new key).
- Unit test (single edit): `insert_text(a)` advances by exactly 1.
- Unit test (retain/evict): with `retain_cap = 4`, 6 edits leave the oldest two
  evicted; `generation_at(evicted)` is `None`.
- Unit test (capture independence): capture at R, then mutate; the captured
  generation still reads the old content.
- **Acceptance gate:** all pass. Commit.

---

## Step 5 — `OverlaySystem`: live and frozen, with shared intern-on-read

**Goal.** One `ty` `System` implementation serving two roles, and the heart of the
gate: a frozen view must be isolated, and any disk read it performs must intern
into the **revision-owned generation shared by all readers of that revision** —
never into per-snapshot private state (SPEC §1.3.1, §1.4, §2.3.3).

**Files.** `rust/src/overlay.rs`.

**Build the type.**
```rust
struct OverlaySystem {
    content: Arc<ArcSwap<Generation>>,  // shared cell; all clones see the same content
    native:  Arc<dyn System + Send + Sync + RefUnwindSafe>,
    frozen:  Option<Revision>,          // None = live head; Some(R) = pinned snapshot
    capture_version: Arc<AtomicU64>,    // versions for disk reads interned by a frozen view
}

impl OverlaySystem {
    pub fn live(root: SystemPathBuf, initial: Generation) -> Self { /* frozen: None */ }
    pub fn frozen(root: SystemPathBuf, gen: Generation, r: Revision) -> Self { /* frozen: Some(r) */ }
    pub fn publish(&self, gen: Generation) { debug_assert!(self.frozen.is_none()); self.content.store(gen); }
    pub fn is_frozen(&self) -> bool { self.frozen.is_some() }
}
```

**The critical decision — where lazy disk reads go.** The architecture pins the
content of overlaid files in the generation, but disk-backed files not yet in the
generation must also become part of revision R when first read. There are two
acceptable designs; **implement Design A** unless the team explicitly opts into B:

- **Design A (preferred): pre-population at snapshot build.** When a snapshot is
  built (Step 8), the project's file set for R is enumerated *once* against a
  consistent disk view and every project file's content is interned into a
  generation derived from R's captured generation, which is then frozen into the
  snapshot's `content` cell. The frozen `read_to_string` then has **no disk
  fallback at all** — a miss is `not_found`. This makes §1.3.1 structural: the
  snapshot's content is fixed at build time and cannot drift.
- **Design B (only if A is too slow): shared lazy intern.** The frozen view may
  read disk on a miss, but it **must** intern the result into the generation that
  is shared by all snapshots of revision R (held in the store's `retained[R]`),
  via a compare-and-swap that keeps an existing entry. This requires the store to
  hand snapshots an `Arc<ArcSwap<Generation>>` *for that revision*, shared across
  all snapshots at R — not a fresh per-snapshot cell.

> Per-snapshot private lazy capture (a fresh `ArcSwap` per snapshot instance)
> is **forbidden** — it is exactly the failure mode in §1.4 and breaks §1.3.1.

For this guide, proceed with **Design A**. The frozen `read_to_string`:
```rust
None if self.frozen.is_some() => Err(not_found(path)),  // pinned: no disk fallback
None => self.native.read_to_string(path),               // live head only
```

**Implement the `System` trait.** Mirror ty's `LSPSystem`:
- `read_to_string`, `path_metadata`, `source_type`: overlay-first
  (`Document::Text` → content/derive metadata from `version`; `Document::Deleted`
  → `not_found`); for `frozen`, a miss is `not_found`; for live, fall through to
  `native`.
- `read_virtual_path_to_string`, `virtual_path_source_type`: from `virtual_files`,
  else native.
- Metadata file revision **MUST** derive from the document `version` so it matches
  the content (a read and its metadata never disagree).

**Validate.**
- Port/confirm these unit tests in `overlay.rs`:
  - live read falls through to disk when not overlaid;
  - overlay text shadows disk;
  - delete tombstone hides a disk file (read and metadata both error);
  - a frozen view built from a generation reads its pinned content and is
    unaffected by later head edits (isolation).
- **New required test (the gate):** build a frozen view at R over a generation
  that already contains file `a` (via Step 8's pre-population in the integration
  test; for this unit test, pre-insert `a` into the generation handed to
  `frozen`). Mutate disk for `a`. Assert the frozen view still returns the pinned
  content — **with Design A there is no disk read to race.**
- **Acceptance gate:** all pass. Commit.

---

## Step 6 — Directory membership is pinned at a revision

**Goal.** A frozen snapshot enumerating a directory observes exactly the entries
present at its revision, including tombstoned absences (SPEC §1.3.3).

**Files.** `rust/src/overlay.rs`.

**Build.** With **Design A**, the snapshot's generation already contains the full
project file set for R (interned at build, Step 8). Implement frozen
`read_directory` / `walk_directory` to enumerate **from the generation**, not from
`native`:
- For a frozen view, list entries whose keys in `content.system` are direct
  children (for `read_directory`) or descendants (for `walk_directory`) of `path`
  and are `Document::Text` (tombstones are absent). Produce `DirectoryEntry`
  values with `FileType::File` (and synthesise intermediate `Directory` entries
  from key prefixes as ty expects).
- For a live view, delegate to `native` (head reads disk directly).

> If full generation enumeration is impractical for very large trees under Design
> A, this is the trigger to revisit Design B *with the shared-generation intern* —
> escalate rather than silently delegating frozen enumeration to live disk.

**Validate.**
- Unit test: a frozen generation containing `{root/a.py, root/sub/b.py}` enumerates
  `a.py` and `sub/` for `read_directory(root)`, and `a.py`+`sub/b.py` for
  `walk_directory(root)`.
- Unit test: a tombstoned path does **not** appear in either enumeration.
- Unit test (the gate): create `root/c.py` on disk *after* building the frozen
  view; assert it does **not** appear in the frozen enumeration; assert it *does*
  appear in a live view's enumeration.
- **Acceptance gate:** all pass. Commit.

---

## Step 7 — Disk ingest writes content into the generation

**Goal.** `sync_path` and the watcher make disk changes **revision-producing events
that record content** (SPEC §1.3.2). Reading a synced revision must be stable even
if disk changes again afterward.

**Files.** `rust/src/project.rs` (sync/watch paths), using `ContentStore::apply_batch`.

**Build.**
- `sync_path(path)`: read disk **once** now. If the file exists, produce
  `Change::Insert { path, text }` with the disk content; if absent, produce
  `Change::Delete { path }` (tombstone). Apply via `apply_batch` so the new
  revision's generation *contains* the disk content/tombstone. (Do **not** merely
  `Forget` and let reads fall through to disk later.)
- Watcher batch (`apply_watch_events`): for each non-overlaid path in the batch,
  read disk once and emit `Insert`/`Delete` `Change`s; drop events for paths with a
  live overlay (overlay wins); a `Rescan` event in the batch ⇒ set the
  `rescan` flag and apply a single rescan. Apply the whole batch via one
  `apply_batch` (one revision per drained batch).
- After mutating the store, the commit transaction (Step 9) publishes and applies
  to the engine. Synthesise the `ChangeEvent`s for the engine from the same
  `Change`s so the store key and the event path are identical.

**Validate.**
- Python/integration test (deterministic seam): `_inject_changes` a disk edit to
  `a.py`; capture the resulting revision R; mutate `a.py` on disk again; open
  `snapshot(at=R)`; assert it reads the *first* synced content, not the second.
- Test: a watcher event for a path with a live overlay buffer is ignored (buffer
  wins).
- Test: `sync_path` on a deleted file yields a revision whose `snapshot` reports
  the path absent (read + directory enumeration).
- **Acceptance gate:** all pass. Commit.

---

## Step 8 — Snapshot capture: build a frozen, pre-populated, independent DB

**Goal.** `snapshot(at=R)` builds an independent analysis database over a frozen
overlay whose generation contains R's full pinned content, and raises a typed error
for an evicted revision (SPEC §1.3.5, §1.3.6, §2.3.3).

**Files.** `rust/src/project.rs`.

**Build.**
- `snapshot(at: Option<Revision>)`:
  1. Resolve R: `None` → `store.revision()`; `Some(r)` → must be retained, else
     return a typed `RevisionEvicted` error (define a `RevisionEvicted` PyError).
  2. `gen = store.generation_at(R)` (the overlaid content at R).
  3. **Pre-populate (Design A):** enumerate the project file set for R against the
     native disk *once*, and for every project file not already in `gen`, read its
     disk content once and insert a `Document::Text` into a derived generation.
     Include directory structure implicitly via the file keys. The result is
     `gen_full`: a generation that fully describes R.
  4. Build `OverlaySystem::frozen(root, gen_full, R)`.
  5. Build an independent `ProjectDatabase` over that system (discover → apply
     config → fallible, with a defaults fallback). This database has its **own**
     storage and can never block or be blocked by the head (SPEC §2).
  6. Return a `Snapshot` handle wrapping the db + revision, with explicit
     `close()`/context-manager/`Drop` semantics.
- Factor the discover→config→build chain into one `build_database(root, system,
  label)` used by both head construction and snapshot construction (no
  duplication).

**Validate.**
- Test: `snapshot(at=current)` then 10 head edits; the snapshot still reads the
  pinned revision across `read`, `metadata`, and directory enumeration.
- Test: `snapshot(at=evicted_R)` raises `RevisionEvicted` (catchable), not a panic.
- Test: two snapshots at the same R, with disk mutated between their creation,
  return identical content and identical directory listings for every project file
  (**the §1.3.1 gate, end-to-end**).
- **Acceptance gate:** all pass. Commit.

---

## Step 9 — Wire writes through the commit transaction (publish last)

**Goal.** Every write mutates the store, applies to the head engine, and publishes
the new generation as the **last** in-lock step, so a reader observing R sees fully
consistent content (SPEC §3.3.3; full transaction is Gate-3 territory, but content
ordering is established here).

**Files.** `rust/src/project.rs`.

**Build.**
- A single `commit(head, changes) -> SyncResult`:
  1. `revision = head.store.apply_batch(changes)`,
  2. `head.system.publish(head.store.capture())` — publish the new generation,
  3. `head.db.apply_changes(&events, None)` — engine incremental update (events
     synthesised from the same `changes`),
  4. build the `SyncResult { revision, created, changed, deleted, rescan, ... }`.
- Route `edit`, `edit_many`, `edit_virtual`, `sync_path`, `apply_watch_events`
  through `commit` with the appropriate `Vec<Change>` and event set. `edit_many`
  builds one `Vec<Change>` ⇒ one `apply_batch` ⇒ one revision (re-confirms §6.1.1
  at the API boundary).
- Hold the existing write lock across steps 1–3; publish before returning.

**Validate.**
- Test: `edit_many({a, b})` returns a `SyncResult.revision` exactly one greater
  than before; no intermediate revision is observable via `snapshot(at=...)`.
- Test: after any single write returns revision R, an immediately-opened
  `snapshot(at=R)` reads the new content for every changed path (publish-before-
  return holds).
- **Acceptance gate:** all pass. Commit.

---

## Step 10 — Remove disk-fallback escape hatches & finalize invariants

**Goal.** Ensure no code path lets a *frozen* read or enumeration touch live disk,
and that comments/docs state the established invariants (SPEC §1.4, §13.1.2).

**Files.** `rust/src/overlay.rs`, `rust/src/project.rs`.

**Build / audit.**
- Grep the frozen branches of `overlay.rs` for any `self.native.*` call reachable
  when `frozen.is_some()`. Under Design A there must be **none** for content or
  directory enumeration. (`canonicalize_path`, `which`, `current_directory` may
  still delegate — they are not revision content; document why each is exempt.)
- Replace any `eprintln!` operational warnings with a logging facade call
  (`log::warn!`); add `log` to `Cargo.toml` if needed (SPEC §13.1.2).
- Update the write-path doc comment to describe the current model: HEAD mutated in
  place; snapshots are independent frozen databases that never block or are blocked
  by writes.

**Validate.**
- `devenv shell -- test-rust` and `devenv shell -- tests` both green.
- Manual audit checklist (paste results into the commit message):
  - [ ] no `native` content/dir read reachable in a frozen view;
  - [ ] no `eprintln!` for operational warnings;
  - [ ] write-path comment accurate.
- **Acceptance gate:** checklist complete, suites green. Commit.

---

## Gate 1 — Final acceptance (must all pass before Gate 2)

Run `devenv shell -- tests` and confirm a dedicated test module proves each:

1. **Same-revision determinism (§1.3.1).** Two snapshots at R, disk mutated
   between them → identical content and directory listings.
2. **Membership pinning (§1.3.3).** A file created on disk after a snapshot is
   absent from that snapshot's enumeration; present in a live view.
3. **Ingest records content (§1.3.2).** `snapshot(at=synced_R)` is stable after a
   later disk change.
4. **Batch atomicity (§6.1.1).** `edit_many` advances the revision exactly once;
   no half-batch revision exists.
5. **Eviction error (§1.3.6).** `snapshot(at=evicted)` raises `RevisionEvicted`.
6. **Isolation/liveness carried (§2).** With Gate-1 changes, the existing
   concurrency stress test (N held snapshots + hot writer) still shows the writer
   never blocking and reads never cancelled.
7. **O(1) publish (§1.3.4).** Publishing over a large project does no per-file
   work (assert via a timing or instrumentation test).

When all seven pass on a clean `devenv shell -- tests`, tag the commit
`gate1-complete` and proceed to Gate 2.

## Rollback / escalation

- If Design A pre-population proves too slow on a real project (Step 5/8),
  **stop and escalate** to adopt Design B *with a shared-per-revision generation
  cell* — do not fall back to per-snapshot private capture or live-disk frozen
  reads. The shared-generation requirement is non-negotiable (§1.4).
- If a validation cannot be made to pass, revert to the last `gate1: step N`
  commit and raise the specific invariant that resists; it likely indicates a
  missed ordering in the commit transaction (Step 9) or a stray disk fallback
  (Step 10).


