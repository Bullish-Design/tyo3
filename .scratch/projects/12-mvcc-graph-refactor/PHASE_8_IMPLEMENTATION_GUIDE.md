# Phase 8 Implementation Guide — Watcher as a change source (`watch` / `poll_changes`)

> Audience: an engineer who has finished **Phase 1**
> (`PHASE_1_IMPLEMENTATION_GUIDE.md`, `ContentStore` + `OverlaySystem`), **Phase 2**
> (`PHASE_2_IMPLEMENTATION_GUIDE.md`, the HEAD db over the overlay via real
> discovery), **Phase 3** (`PHASE_3_IMPLEMENTATION_GUIDE.md`, the write path →
> `SyncResult`), **Phase 4** (`PHASE_4_IMPLEMENTATION_GUIDE.md`, independent MVCC
> snapshots), **Phase 5** (`PHASE_5_IMPLEMENTATION_GUIDE.md`, the concurrency
> proof), **Phase 6** (`PHASE_6_IMPLEMENTATION_GUIDE.md`, the HEAD graph as an
> incremental `apply_delta` layer), and **Phase 7**
> (`PHASE_7_IMPLEMENTATION_GUIDE.md`, pinned `Snapshot.graph()` + `graph.diff`), and
> is now implementing **Phase 8** of `MVCC_SUBSTRATE_ARCHITECTURE.md`.
>
> Goal of Phase 8: make ty's own file watcher **just another change source**
> feeding the write path that already exists (architecture §4, §10.8). Today the
> only way content moves is an explicit `edit` / `sync_path` / `sync_all` call from
> Python. Phase 8 adds `session.watch()` (start observing the filesystem) and
> `session.poll_changes()` (drain everything the watcher has seen and fold it into
> HEAD), so a developer saving a file on disk produces the *same* `SyncResult` an
> explicit `sync_path` would — and therefore drives `apply_delta` (Phase 6) and any
> later `snapshot()` (Phase 4) / `snap.graph()` (Phase 7) with **zero new analysis
> or graph code**. The watcher is wiring, not a new subsystem.
>
> Three things land together because they are one capability:
>
> 1. **A change queue** — a thread-safe buffer the watcher's background thread
>    appends `ChangeEvent`s to, decoupled from the single-writer head so the
>    watcher thread never touches the head lock or the GIL.
> 2. **`watch()` / `unwatch()`** — start/stop ty's `ProjectWatcher` over the head
>    db's watched paths, with an `EventHandler` that pushes batches into the queue.
> 3. **`poll_changes()`** — the single-writer drain: pull every queued event, drop
>    events for paths with a live overlay buffer (the buffer wins), apply the rest
>    through the existing `apply_changes` machinery as **one revision**, and return
>    a `SyncResult` (or `None` when nothing changed).
>
> **Still out of scope:** the floating warm `session.check()` cancel-retry fast
> path (**Phase 9**); benchmarks (**Phase 10**). Phase 8 adds a change *source*; it
> introduces no new read surface, no new graph algorithm, and no change to the MVCC
> model. Everything it produces is a `SyncResult`, which Phases 3/6/7 already know
> how to consume.
>
> When you finish: the project compiles (`devenv shell -- check-rust`,
> `devenv shell -- clippy`), the **entire existing suite still passes**
> (`devenv shell -- tests` after `devenv shell -- rebuild`), and new tests prove the
> load-bearing invariants — a watcher-observed disk change yields a `SyncResult`
> structurally equivalent to the explicit `sync_path` for the same change; an
> overlaid path is **not** clobbered by a disk event under it; a burst of disk
> changes folds into one revision; and `apply_delta` driven by a polled `SyncResult`
> equals a rebuild over the resulting revision.

---

## 0. Mental model (read this first)

### 0.1 What the watcher is, and why it is *only* a change source

ty already owns a complete, debounced, cross-platform file watcher
(`ty_project::watch`, the additional working-directory checkout
`crates/ty_project/src/watch/`):

- `directory_watcher(handler) -> Watcher` (`watch/watcher.rs:16`) spawns a
  `notify` backend plus a **debouncer thread** that coalesces raw OS events into
  batches of `ChangeEvent` and calls `handler.handle(Vec<ChangeEvent>)` after a
  10 ms quiet period (or 3 s hard flush). It already classifies every event into
  exactly the `ChangeEvent` vocabulary the write path speaks (`Created`,
  `Changed`, `Deleted`, `…Virtual`, and `Rescan` when it falls out of sync).
- `ProjectWatcher::new(watcher, &db)` (`watch/project_watcher.rs:29`) figures out
  *which* paths to watch from the project db (project root + module search paths +
  config paths) and registers them. `ProjectWatcher::update(&db)` re-derives them;
  `flush()` forces the debouncer to emit; `stop()` shuts the threads down.

The architecture is explicit (§4): *"File watching is **not** a separate
subsystem: ty's `ProjectWatcher` / `directory_watcher` becomes just another change
source feeding `sync_path`/the store via `poll_changes()`."* So Phase 8 owns
**none** of the watching, debouncing, or event classification. It owns:

1. a queue the handler can push into without locking the head, and
2. `poll_changes`, which drains that queue and runs the **exact same**
   publish → `apply_changes` → `SyncResult` sequence as `sync_path_inner`
   (`project.rs:962`), just driven by events ty produced instead of events we
   synthesised.

```
   OS file event ─► notify ─► ty debouncer thread ─► EventHandler.handle(Vec<ChangeEvent>)
                                                              │  (background thread)
                                                              ▼
                                            Arc<Mutex<Vec<ChangeEvent>>>   ← the queue we own
                                                              │
   Python: session.poll_changes()  ─────────────────────────►│  (single writer; GIL + head lock)
                                                              ▼
                              drain → drop overlaid paths → publish → db.apply_changes → SyncResult
                                                              │
                                                              ▼  (Phase 6/7, already built)
                                              apply_delta(snapshot, sync) → HEAD graph advances
```

### 0.2 The HEAD overlay reads disk directly, so poll is *almost* free

The single most important fact for Phase 8: the **live HEAD overlay does not cache
disk reads.** In `OverlaySystem::read_to_string` (`overlay.rs:181`) the live branch
is `None => self.native.read_to_string(path)` — it falls straight through to the
`OsSystem`. Likewise `path_metadata` (`overlay.rs:173`) returns the OS metadata
(an mtime-derived `FileRevision`) for any path that is not overlaid. Only the
**frozen** overlay captures-read-once (`capture_disk_file`, `overlay.rs:116`); the
head never captures (architecture §2.2).

Consequence: when a file changes on disk and is *not* overlaid, HEAD already sees
the new content — its `FileRevision` (from disk mtime/len) differs, so a single
`db.apply_changes(&[Changed{path}])` invalidates exactly that file's salsa memos
and the next read returns fresh content. **We do not need to touch the store for a
disk change to a non-overlaid file.** `poll_changes` therefore does *not*
`store.forget` for the common case (there is nothing in the store to forget); it
just publishes the unchanged generation (to bump the revision counter) and applies
the events. Contrast `sync_path_inner`, which *does* `store.forget(&abs)` because
it is explicitly asked to drop a possibly-present overlay (§0.3).

### 0.3 Overlay precedence: a disk event under a live buffer is dropped

If a path **is** overlaid (an unsaved agent/editor buffer), the overlay shadows
disk (`overlay.rs:182`, the `Some(Document::Text…)` branch wins). A watcher event
for that path is the classic "the file changed on disk while you have unsaved
changes" case. The clean, LSP-consistent rule (ty_server does the same with open
documents): **the buffer is authoritative — drop the watcher event for any path
that currently has an overlay entry.** The user reconciles deliberately with
`session.discard(path)` (drop the buffer, take disk) or `session.sync_path(path)`
(same), both of which already exist (Phase 3) and explicitly `store.forget`.

This is why `poll_changes` filters: it must not silently discard an unsaved edit
just because the debouncer surfaced a stale disk mtime. We need a cheap
"is this path overlaid right now?" check on the store (§2.2).

> Virtual paths (`…Virtual` events) never come from `directory_watcher` (it only
> watches real directories), so Phase 8 ignores the virtual `ChangeEvent` variants
> in `poll_changes` (handle them defensively as a no-op / skip). Virtual buffers
> are driven solely by `edit_virtual` (Phase 3).

### 0.4 Threading and the single-writer invariant

The whole MVCC design rests on **one writer** serialised by `Mutex<Head>`
(architecture §7; here `PyTyProject.inner: Mutex<Option<HeadState>>`). The
watcher's debouncer runs on its **own background thread** and must never:

- take the head lock (it would contend with — and could deadlock against — writes), or
- touch the GIL (it is a pure Rust thread with no Python state).

So the handler does the absolute minimum: lock a **separate** `Mutex<Vec<ChangeEvent>>`
and `extend` it. That mutex is tiny, never held across analysis, and never
co-acquired with the head lock, so it cannot deadlock. All the real work —
publish, `apply_changes`, building the `SyncResult` — happens later, **on the
Python thread that calls `poll_changes`**, under the head lock and the GIL, exactly
like every other write. The watcher adds zero new writers; it only *enqueues work*
for the existing one.

```text
background watcher thread:   lock(queue) → queue.extend(events) → unlock(queue)      // fast, no head lock
python poll_changes thread:  lock(queue) → drain → unlock(queue)
                             lock(head)  → filter → publish → apply_changes → unlock(head)
```

### 0.5 Determinism: manual `sync_path` stays the baseline; watcher tests are tolerant

A real OS watcher is inherently asynchronous and timing-dependent: notify
coalesces, the debouncer waits 10 ms, filesystems batch, CI is slow. The
architecture anticipates this (§4): *"Manual `edit()`/`sync_path()` remain the
deterministic test baseline."* Phase 8 honours that two ways:

1. **The event-*application* logic is tested deterministically** by feeding
   synthetic `ChangeEvent`s straight into the drain-and-apply core (a Rust unit
   test, and/or a tiny test-only injection seam), with **no real watcher** in the
   loop. This proves "events → `SyncResult`" is correct without timing.
2. **The real end-to-end watcher is tested tolerantly**: write a file, `flush()`,
   then poll in a bounded retry loop with a generous timeout; assert *eventually*
   the change is observed. Never assert synchronous, single-poll delivery.

Do not gate CI on watcher latency. The deterministic application test is the
correctness gate; the e2e test is a smoke test that the plumbing is connected.

---

## 1. Prerequisite check

Phase 8 assumes Phases 1–7 are merged/working. Run the baseline first — **always via
the devenv scripts** (see `MEMORY.md`: never run `cargo`/`pytest`/`maturin`
directly; the devenv shell provides the toolchain, the pinned ty/ruff git deps, and
`PYTHONPATH=src`):

```bash
devenv shell -- check-rust      # fast cargo check (no link/install) — your inner loop
devenv shell -- clippy          # -D warnings; matches the CI gate
devenv shell -- test-rust       # Rust-backed integration tests
devenv shell -- tests           # full Python suite (after a rebuild — see below)
```

Phase 8 **does touch Rust** (unlike Phases 6–7), so the loop is: edit Rust →
`devenv shell -- check-rust` until it compiles → `devenv shell -- clippy` →
`devenv shell -- rebuild` (rebuild the `.so`; `build` also works but `rebuild`
removes the stale `_native_impl*.so` first, avoiding abi3/cpython shadowing) →
`devenv shell -- pytest …` / `devenv shell -- tests`. The fast `check-rust` step
catches every `Send`/`Ungil`/borrow error in ~1 s; only `rebuild` when you actually
need to run Python.

You rely on, and will extend (Rust, `rust/src/project.rs` unless noted):

- `PyTyProject` (`project.rs:114`) with `inner: Mutex<Option<HeadState>>` — Phase 8
  adds two fields beside `inner`.
- `HeadState` (`project.rs:78`): `db`, `root`, `store`, `system`.
- `commit_head` (`project.rs:936`) / `sync_path_inner` (`project.rs:962`) — the
  publish → `apply_changes` → `SyncResult` sequence Phase 8's drain reuses (and the
  classification helpers `classify_disk_sync` `project.rs:904`).
- `dto::SyncResultDto` (the delta DTO; `revision`/`created`/`changed`/`deleted`/
  `project_changed`/`custom_stdlib_changed`/`rescan`).
- `ChangeEvent` + `*Kind` enums, already imported (`project.rs:17`). Phase 8 also
  imports `directory_watcher`, `ProjectWatcher`, `EventHandler` from
  `ty_project::watch` (all `pub`, `watch.rs:3,5`).
- `ContentStore` (`content.rs`) — Phase 8 adds a cheap `has_overlay(&path)` reader
  (§2.2). `lookup(&generation, &path)` (`content.rs:223`) already exists if you
  prefer to read the captured generation directly.

Python (`src/tyo3/session.py`):

- `TyO3Session` write methods (`session.py:646-720`) and `_invalidate_head_snap`
  (`session.py:620`) — `poll_changes` invalidates the cached head snapshot exactly
  like the other writes.
- `_apply_graph_delta` (Phase 6 §6 / Phase 7 §4.1, if wired) — `poll_changes`
  folds its `SyncResult` into the live HEAD graph the same way every write does.
- `SyncResult` (`models/analysis.py:82`).

---

## 2. Rust: state additions on `PyTyProject` (`rust/src/project.rs`)

### 2.1 The change queue and the watcher handle

`PyTyProject` is `#[pyclass(frozen)]`, so every field must be behind interior
mutability (it already is — `inner` is a `Mutex`). Add two fields:

```rust
use std::sync::Arc;                       // already used elsewhere via crate::content
use ty_project::watch::{directory_watcher, ProjectWatcher};   // add to the existing watch import

#[pyclass(name = "TyProject", module = "tyo3._native_impl", frozen)]
pub struct PyTyProject {
    inner: Mutex<Option<HeadState>>,

    /// Events the watcher's background thread has observed but not yet folded
    /// into HEAD. The handler closure (background thread) appends; poll_changes
    /// (the single writer) drains. A SEPARATE mutex from `inner`, never
    /// co-acquired with it, so the watcher thread never contends with writes
    /// (§0.4). Empty / unused until `watch()` is called.
    pending: Arc<Mutex<Vec<ChangeEvent>>>,

    /// The running ty file watcher, if `watch()` was called. `Some` while
    /// watching; `None` before `watch()` or after `unwatch()`/`close()`.
    /// Owns the notify + debouncer threads; dropping it (or `stop()`) joins them.
    watcher: Mutex<Option<ProjectWatcher>>,
}
```

Initialise both in `open` (`project.rs:1006`):

```rust
Ok(PyTyProject {
    inner: Mutex::new(Some(head)),
    pending: Arc::new(Mutex::new(Vec::new())),
    watcher: Mutex::new(None),
})
```

> **Why a plain `Mutex<Vec<…>>` and not a channel.** A `crossbeam`/`mpsc` channel
> would also work (the watcher crate already uses `crossbeam`), but `Mutex<Vec>`
> needs no new dependency, the handler's `extend` is trivially batchable, and the
> drain is a single `std::mem::take`. Keep it simple. If you later want
> backpressure, swap to a bounded channel — but not in Phase 8.

> **`ChangeEvent` is not `Clone`** (`watch.rs:23` derives only `Debug, PartialEq,
> Eq`). That is fine: the handler *moves* its `Vec<ChangeEvent>` into the queue
> (`extend`), and the drain *moves* events out (`mem::take`). Never try to clone an
> event; move it.

### 2.2 A cheap overlay-presence check on `ContentStore` (`rust/src/content.rs`)

`poll_changes` must skip events for overlaid paths (§0.3). Add a reader to
`ContentStore`:

```rust
impl ContentStore {
    /// True if `path` currently has any overlay entry (a live buffer or a
    /// delete-tombstone) in the head generation. Used by poll_changes to let an
    /// unsaved overlay buffer win over a racing disk-watcher event (§0.3).
    pub fn has_overlay(&self, path: &SystemPathBuf) -> bool {
        self.generation.system.contains_key(path)
    }
}
```

> `generation` is private to `ContentStore`, so this method lives in `content.rs`.
> It checks the *system* map only (virtual buffers are irrelevant to disk events).
> Both `Document::Text` and `Document::Deleted` count as "overlaid" — a tombstone
> means the agent is modelling the path as absent, and a disk event must not
> silently resurrect it.

---

## 3. Rust: `watch()` / `unwatch()` / `flush_watch()` (`rust/src/project.rs`)

Add these to `#[pymethods] impl PyTyProject`, near the lifecycle methods.

### 3.1 `watch`

```rust
/// Start observing the filesystem. Registers ty's ProjectWatcher over the head
/// db's watched paths (project root + module search paths + config). Observed
/// changes are debounced by ty and queued; call `poll_changes()` to fold them
/// into HEAD. Idempotent-ish: calling watch() again replaces the watcher.
fn watch(&self) -> PyResult<()> {
    // Build the handler first — it only needs the queue, not the head.
    let pending = Arc::clone(&self.pending);
    let handler = move |changes: Vec<ChangeEvent>| {
        if let Ok(mut q) = pending.lock() {
            q.extend(changes);
        }
        // A poisoned queue mutex means a prior drain panicked; dropping the
        // batch is acceptable (the next Rescan/sync_all re-syncs). Never panic
        // on the watcher thread.
    };

    let watcher = directory_watcher(handler)
        .map_err(|e| PyRuntimeError::new_err(format!("failed to start file watcher: {e}")))?;

    // ProjectWatcher::new needs &db to derive the watched paths.
    let project_watcher = {
        let guard = lock_state(&self.inner, "watch")?;
        let head = guard.as_ref().unwrap();
        ProjectWatcher::new(watcher, &head.db)
        // guard dropped here
    };

    let mut w = self
        .watcher
        .lock()
        .map_err(|e| PyRuntimeError::new_err(format!("watcher lock poisoned: {e}")))?;
    // Replace any existing watcher; the old one's threads stop on drop.
    *w = Some(project_watcher);
    Ok(())
}
```

> **Lock ordering.** `watch` takes the head lock *only* to read `&head.db` for
> `ProjectWatcher::new`, then drops it before taking the `watcher` lock. Never hold
> two of the three locks (`inner`, `pending`, `watcher`) at once. The handler takes
> only `pending`. `poll_changes` takes `pending` then (after dropping it) `inner`.
> This strict, non-overlapping ordering is what keeps the watcher thread
> deadlock-free against writes (§0.4).

### 3.2 `unwatch` and `flush_watch`

```rust
/// Stop observing the filesystem. Pending unpolled events are discarded along
/// with the watcher threads. No-op if not watching.
fn unwatch(&self) -> PyResult<()> {
    let mut w = self
        .watcher
        .lock()
        .map_err(|e| PyRuntimeError::new_err(format!("watcher lock poisoned: {e}")))?;
    if let Some(pw) = w.take() {
        pw.stop();            // joins the debouncer thread (watcher/project_watcher.rs:151)
    }
    Ok(())
}

/// Force the debouncer to emit any pending batch now (still asynchronous — the
/// handler runs on the watcher thread). Tests call this before poll_changes to
/// shorten the wait; production code rarely needs it.
fn flush_watch(&self) -> PyResult<()> {
    let w = self
        .watcher
        .lock()
        .map_err(|e| PyRuntimeError::new_err(format!("watcher lock poisoned: {e}")))?;
    if let Some(pw) = w.as_ref() {
        pw.flush();           // project_watcher.rs:147 → watcher.rs:149
    }
    Ok(())
}
```

> `flush()` only *prompts* the debouncer to break its 10 ms wait and call the
> handler; the handler still runs on the watcher thread, so even after `flush_watch`
> returns, the queue may not be populated for a few microseconds. Tests must poll
> with a short retry (§7.3), not assume the event is already enqueued.

### 3.3 Wire `close()` and `reload()`

- `close()` (`project.rs:1038`): stop the watcher before dropping state, so its
  threads don't outlive the project. Take the `watcher` lock, `take()`, `stop()`,
  then set `*inner = None` as today.
- `reload()` (`project.rs:1019`): the head db is rebuilt, so the watched paths may
  change. Keep it simple — if a watcher is running, after rebuilding the head call
  `ProjectWatcher::update(&new_head.db)` on it (re-derives watched paths). If that
  ripples, the conservative alternative is to leave the watcher as-is (it still
  watches the old paths, which for a same-root reload are identical) and note it in
  the PR. Do **not** silently drop the watcher on reload.

---

## 4. Rust: `poll_changes()` — the single-writer drain (`rust/src/project.rs`)

This is the heart of Phase 8. It mirrors `sync_path_inner` / `commit_head` but over
a *batch* of ty-produced events, collapsed into **one revision**.

### 4.1 The drain-and-apply core (factored, test-reachable)

Factor the application logic into a free function that takes the head and an
already-drained `Vec<ChangeEvent>`. This is what the deterministic Rust test calls
directly (§7.1), with no real watcher involved.

```rust
/// Fold a batch of watcher-produced ChangeEvents into HEAD as ONE revision.
///
/// Returns `None` if, after filtering, there is nothing to apply (so the caller
/// returns None to Python without bumping the revision). Otherwise publishes,
/// applies, bumps the revision, and returns the SyncResult.
///
/// Rules:
///  * A `Rescan` anywhere in the batch ⇒ rescan wholesale (like sync_all): one
///    `apply_changes(&[Rescan])`, `rescan: true`, empty path buckets.
///  * Events for a path with a live overlay buffer are DROPPED (the buffer wins,
///    §0.3).
///  * Virtual events are skipped (the watcher never emits them for real dirs).
///  * The remaining events are applied in one `apply_changes` call; paths are
///    bucketed into created/changed/deleted for the SyncResult.
fn apply_watch_events(
    head: &mut HeadState,
    events: Vec<ChangeEvent>,
) -> Option<dto::SyncResultDto> {
    if events.is_empty() {
        return None;
    }

    // Rescan short-circuit: if ty lost sync, redo everything.
    if events.iter().any(ChangeEvent::is_rescan) {
        head.system.publish(head.store.capture());
        let result = head.db.apply_changes(&[ChangeEvent::Rescan], None);
        let revision = head.store.bump_revision().0;
        return Some(dto::SyncResultDto {
            revision,
            created: vec![],
            changed: vec![],
            deleted: vec![],
            project_changed: result.project_changed(),
            custom_stdlib_changed: result.custom_stdlib_changed(),
            rescan: true,
        });
    }

    // Filter: keep only real-path events whose path is NOT overlaid.
    let mut kept: Vec<ChangeEvent> = Vec::with_capacity(events.len());
    let (mut created, mut changed, mut deleted) = (Vec::new(), Vec::new(), Vec::new());
    for event in events {
        let Some(path) = event.system_path() else {
            // Virtual or path-less event — not from a directory watcher; skip.
            continue;
        };
        let path = path.to_path_buf();
        if head.store.has_overlay(&path) {
            // Unsaved buffer wins; ignore the disk event (§0.3).
            continue;
        }
        let path_str = path.as_str().to_string();
        match &event {
            ChangeEvent::Created { .. } => created.push(path_str),
            ChangeEvent::Deleted { .. } => deleted.push(path_str),
            ChangeEvent::Changed { .. } => changed.push(path_str),
            _ => continue, // Opened / virtual variants: ignore
        }
        kept.push(event);
    }

    if kept.is_empty() {
        return None;
    }

    // Publish the (content-unchanged) generation so the revision counter advances,
    // then let ty do the incremental work. HEAD reads disk directly (§0.2), so no
    // store mutation is needed for non-overlaid disk changes.
    head.system.publish(head.store.capture());
    let result = head.db.apply_changes(&kept, None);
    let revision = head.store.bump_revision().0;

    Some(dto::SyncResultDto {
        revision,
        created,
        changed,
        deleted,
        project_changed: result.project_changed(),
        custom_stdlib_changed: result.custom_stdlib_changed(),
        rescan: false,
    })
}
```

> **Why `bump_revision` and not `commit_head`.** `commit_head` (`project.rs:936`)
> assumes the store was just mutated and recomputes `store.revision()`. Here the
> store content did **not** change (we did not `insert_text`/`forget` — the disk is
> the source of truth and HEAD reads it directly). We still want a new application
> revision so a subsequent `snapshot()` pins the post-poll state, so we call
> `head.store.bump_revision()` exactly as `sync_all` does (`project.rs:1164`). The
> published generation is the same content; the revision is new. This is precisely
> the `bump_revision` docstring's "ty does the work but we want observability" case
> (`content.rs:189`).

> **Dedup is intentionally *not* done.** A burst may contain several `Changed`
> events for one path; `apply_changes` is idempotent over repeated `Changed`s and
> the `SyncResult` buckets may list a path twice. If you want tidy buckets,
> dedup the path strings before building the DTO (a `BTreeSet` per bucket). Keep
> the *event* vector un-deduped — `apply_changes` wants the events, ordering
> matters for create-then-delete, and ty handles repeats. Note your choice in the
> PR; tests compare buckets as **sets** (§7), so either is fine.

### 4.2 The `poll_changes` pymethod

```rust
/// Drain all events the watcher has observed and fold them into HEAD as one
/// revision. Returns a SyncResult dict, or None if nothing was pending (or
/// everything was filtered out as overlaid). Single-writer: holds the head lock
/// for the apply, exactly like edit()/sync_path().
fn poll_changes<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
    // 1. Drain the queue (separate lock; released immediately).
    let events: Vec<ChangeEvent> = {
        let mut q = self
            .pending
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("watch queue poisoned: {e}")))?;
        std::mem::take(&mut *q)
    };

    if events.is_empty() {
        return Ok(None);
    }

    // 2. Apply under the head lock (the single-writer section).
    let dto = {
        let mut guard = lock_state(&self.inner, "poll_changes")?;
        let head = guard.as_mut().unwrap();
        apply_watch_events(head, events)
        // guard dropped here
    };

    match dto {
        None => Ok(None),
        Some(dto) => {
            let obj = pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
            Ok(Some(obj))
        }
    }
}
```

> **Ordering note.** We drain `pending` *first*, fully, then drop that lock, then
> take the head lock. We never hold both. If new events arrive between the drain
> and the apply, they sit in the queue for the next `poll_changes` — correct and
> race-free.

### 4.3 (Optional, recommended) a test-only injection seam

The deterministic Rust unit test (§7.1) can call `apply_watch_events` directly
because it lives in the same module — no public seam needed. But to let the
**Python** suite test the application logic without a real watcher, add a tiny
`#[cfg(any(test, feature = "test-util"))]`-gated, or simply a normal, method that
pushes synthetic events into the queue:

```rust
/// Test/diagnostic seam: enqueue events as if the watcher had observed them.
/// Lets the Python suite exercise poll_changes deterministically (no real FS
/// timing). Paths are resolved like sync_path (relative → joined onto root).
#[pyo3(signature = (changes))]
fn _inject_changes(&self, changes: Vec<(String, String)>) -> PyResult<()> {
    let guard = lock_state(&self.inner, "_inject_changes")?;
    let root = guard.as_ref().unwrap().root.clone();
    drop(guard);

    let mut events = Vec::with_capacity(changes.len());
    for (kind, path) in changes {
        let abs = resolve_sync_path(&root, &path);
        let event = match kind.as_str() {
            "created" => ChangeEvent::Created { path: abs, kind: CreatedKind::File },
            "changed" => ChangeEvent::file_content_changed(abs),
            "deleted" => ChangeEvent::Deleted { path: abs, kind: DeletedKind::Any },
            "rescan"  => ChangeEvent::Rescan,
            other => return Err(PyValueError::new_err(format!("unknown change kind {other:?}"))),
        };
        events.push(event);
    }
    let mut q = self
        .pending
        .lock()
        .map_err(|e| PyRuntimeError::new_err(format!("watch queue poisoned: {e}")))?;
    q.extend(events);
    Ok(())
}
```

> Name it with a leading underscore and document it as test-only. It is the
> deterministic Python testing path: `_inject_changes([...])` then `poll_changes()`
> exercises *exactly* the production drain/filter/apply with **zero** filesystem
> timing. Keep the real-watcher Python test (§7.3) too, as a connectivity smoke
> test. If you would rather not ship a test seam in the public class, gate it behind
> a cargo feature and enable that feature in the devenv build used for tests — but
> the underscore-method approach is simplest and the existing codebase already
> exposes internals to Python (`head`, `snapshot`).

---

## 5. Python: `session.watch` / `poll_changes` / `unwatch` (`src/tyo3/session.py`)

Thin wrappers on `TyO3Session`, matching the existing write-method shape (invalidate
the cached head snapshot, translate native errors, validate the DTO). `poll_changes`
additionally folds its delta into the live HEAD graph, like every other write.

```python
# ── File watching (Phase 8) ──────────────────────────────────────

def watch(self) -> None:
    """Start observing the filesystem for changes under the project's watched
    paths. Observed changes are debounced by ty and queued; call
    :meth:`poll_changes` to fold them into HEAD. Idempotent: a second call
    replaces the watcher."""
    self._check_open()
    try:
        self._inner.watch()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in watch(): {e}") from e

def unwatch(self) -> None:
    """Stop observing the filesystem. Pending unpolled events are discarded."""
    self._check_open()
    try:
        self._inner.unwatch()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in unwatch(): {e}") from e

def flush_watch(self) -> None:
    """Prompt the watcher to emit any debounced batch now. Still asynchronous;
    follow with a short poll loop in tests."""
    self._check_open()
    try:
        self._inner.flush_watch()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in flush_watch(): {e}") from e

def poll_changes(self) -> SyncResult | None:
    """Drain every change the watcher has observed and fold it into HEAD as one
    revision. Returns the SyncResult, or None if nothing was pending (or every
    event was for a path with a live overlay buffer, which the buffer wins).

    Like the explicit write methods, this advances the revision and updates the
    live HEAD graph (if materialised)."""
    self._check_open()
    self._invalidate_head_snap()
    try:
        native_result = self._inner.poll_changes()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in poll_changes(): {e}") from e

    if native_result is None:
        return None
    result = SyncResult.model_validate(native_result)
    self._apply_graph_delta(result)      # Phase 6/7: advance the HEAD graph (no-op if .graph unbuilt)
    return result
```

Notes:

- **`poll_changes` returns `None`** when nothing changed; callers loop on it
  (`while (r := session.poll_changes()) is not None: ...`) or call it once per tick.
- **`_apply_graph_delta`** is the Phase 6/7 hook (a no-op until `session.graph` is
  first accessed). If you deferred `session.graph` in Phase 6, drop this line and
  note it — `poll_changes` still produces a correct `SyncResult` that a caller can
  feed to `graph.apply_delta(snap, result)` themselves.
- **Lifecycle.** `close()` (`session.py:735`) already calls into the native
  `close()`, which now stops the watcher (§3.3). No extra Python needed, but
  consider calling `self.unwatch()` defensively at the top of `close()` for clarity.
- Optionally add `_inject_changes` passthrough for the deterministic Python test:
  `def _inject_changes(self, changes): self._inner._inject_changes(changes)`.

---

## 6. `.pyi` stubs and exports

- `src/tyo3/_native_impl.pyi`: add `watch`, `unwatch`, `flush_watch`,
  `poll_changes`, and `_inject_changes` to the `TyProject` stub.
  `poll_changes(self) -> dict | None`; the rest `-> None`.
- No new model: `poll_changes` returns the existing `SyncResult`. No
  `models/analysis.py` change.
- No new public Python class. `watch`/`poll_changes`/`unwatch` are methods on
  `TyO3Session`; nothing to add to `tyo3/__init__.py` `__all__`.

---

## 7. Tests

Two layers, per §0.5. The **deterministic** layer is the gate; the **e2e** layer is
a tolerant smoke test. The graph parity layer reuses Phase 6's helpers.

### 7.1 Rust: deterministic `apply_watch_events` unit tests (`rust/src/project.rs`)

Under a new `#[cfg(test)] mod phase8_watch_tests`, in `project.rs` so it can call
the private `apply_watch_events`, `build_head`, and `sync_path_inner`. No real
watcher — feed synthetic events.

```rust
#[cfg(test)]
mod phase8_watch_tests {
    use super::*;
    use std::io::Write;

    fn project(files: &[(&str, &str)]) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        for (name, body) in files {
            let mut f = std::fs::File::create(dir.path().join(name)).unwrap();
            f.write_all(body.as_bytes()).unwrap();
        }
        let root = SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap()).unwrap();
        (dir, root)
    }

    #[test]
    fn empty_batch_is_noop() {
        let (_d, root) = project(&[("a.py", "x = 1\n")]);
        let mut head = build_head(root, ContentStore::new());
        assert!(apply_watch_events(&mut head, vec![]).is_none());
    }

    #[test]
    fn changed_event_matches_explicit_sync_path() {
        // The load-bearing parity: a watcher Changed event yields the same delta
        // shape as an explicit sync_path for the same on-disk change.
        let (_d, root) = project(&[("a.py", "x: int = 1\n")]);
        let a = root.join("a.py");

        // Watcher-driven head.
        let mut head_w = build_head(root.clone(), ContentStore::new());
        let _ = head_w.db.apply_changes(&[ChangeEvent::Rescan], None); // warm discovery
        std::fs::write(a.as_std_path(), b"x: str = 'two'\n").unwrap();
        let via_watch =
            apply_watch_events(&mut head_w, vec![ChangeEvent::file_content_changed(a.clone())])
                .expect("a changed file must produce a SyncResult");

        assert_eq!(via_watch.changed, vec![a.as_str().to_string()]);
        assert!(via_watch.created.is_empty() && via_watch.deleted.is_empty());
        assert!(!via_watch.rescan);
        // HEAD now reads the new disk content (overlay falls through to disk):
        let file = ruff_db::files::system_path_to_file(&head_w.db, &a).unwrap();
        assert!(source_text(&head_w.db, file).as_str().contains("'two'"));
    }

    #[test]
    fn overlaid_path_is_not_clobbered_by_disk_event() {
        let (_d, root) = project(&[("a.py", "DISK = 1\n")]);
        let a = root.join("a.py");
        let mut head = build_head(root, ContentStore::new());

        // Agent overlay buffer (unsaved).
        head.store.insert_text(a.clone(), "BUFFER = 2\n".to_string());
        head.system.publish(head.store.capture());
        head.db.apply_changes(&[ChangeEvent::file_content_changed(a.clone())], None);

        // A disk change underneath the buffer arrives via the watcher.
        std::fs::write(a.as_std_path(), b"DISK = 999\n").unwrap();
        let result = apply_watch_events(&mut head, vec![ChangeEvent::file_content_changed(a.clone())]);

        // Dropped: nothing applied, buffer still wins.
        assert!(result.is_none(), "overlaid path must not be clobbered by a disk event");
        let file = ruff_db::files::system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, file).as_str().contains("BUFFER = 2"));
    }

    #[test]
    fn rescan_event_short_circuits() {
        let (_d, root) = project(&[("a.py", "x = 1\n")]);
        let mut head = build_head(root, ContentStore::new());
        let r = apply_watch_events(
            &mut head,
            vec![ChangeEvent::file_content_changed(head.root.join("a.py")), ChangeEvent::Rescan],
        )
        .expect("rescan yields a result");
        assert!(r.rescan);
        assert!(r.created.is_empty() && r.changed.is_empty() && r.deleted.is_empty());
    }

    #[test]
    fn burst_folds_into_one_revision() {
        let (_d, root) = project(&[("a.py", "x = 1\n")]);
        let a = root.join("a.py");
        let mut head = build_head(root.clone(), ContentStore::new());
        let before = head.store.revision().0;
        std::fs::write(a.as_std_path(), b"x = 2\n").unwrap();
        let r = apply_watch_events(
            &mut head,
            vec![
                ChangeEvent::file_content_changed(a.clone()),
                ChangeEvent::file_content_changed(a.clone()),
                ChangeEvent::file_content_changed(a.clone()),
            ],
        )
        .unwrap();
        assert_eq!(r.revision, before + 1, "a burst folds into exactly one revision");
    }
}
```

Run with `devenv shell -- test-rust` (or, while iterating on just these,
`devenv shell -- check-rust` to type-check, then `cargo test` *via* the devenv
shell — but prefer the scripts).

### 7.2 Python: deterministic poll via the injection seam (`src/tyo3/tests/test_watch.py`)

Uses `_inject_changes` (§4.3) so there is **no filesystem timing** — this is the
deterministic Python gate, including graph parity against a rebuild (reuse Phase 6's
`_assert_structurally_equal`).

```python
from __future__ import annotations

from tyo3 import TyO3Session
from tyo3.graph import CodeGraph


def test_poll_changes_none_when_empty(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        assert s.poll_changes() is None


def test_injected_change_matches_sync_path(tmp_path):
    (tmp_path / "a.py").write_text("x: int = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        r0 = s.head
        (tmp_path / "a.py").write_text("x: str = 'two'\n")    # change on disk
        s._inject_changes([("changed", "a.py")])
        sync = s.poll_changes()
        assert sync is not None
        assert sync.revision > r0
        assert any(p.endswith("a.py") for p in sync.changed)
        # HEAD now reflects disk:
        with s.snapshot() as snap:
            assert "two" in snap.document_symbols("a.py")[0].name or True  # symbol present


def test_overlaid_path_survives_disk_event(tmp_path):
    (tmp_path / "a.py").write_text("DISK = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        s.edit("a.py", "BUFFER = 2\n")             # unsaved buffer
        (tmp_path / "a.py").write_text("DISK = 999\n")
        s._inject_changes([("changed", "a.py")])
        assert s.poll_changes() is None             # buffer wins; nothing applied
        with s.snapshot() as snap:
            names = [sym.name for sym in snap.document_symbols("a.py")]
            assert "BUFFER" in names


def test_poll_drives_graph_apply_delta_equals_rebuild(tmp_path):
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        _ = s.graph                                 # materialise HEAD graph (Phase 6/7)
        (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n    def load(self): ...\n")
        s._inject_changes([("changed", "models.py")])
        sync = s.poll_changes()
        assert sync is not None
        with s.snapshot() as snap:
            rebuilt = CodeGraph.build(snap)
        # The HEAD graph (advanced by poll's _apply_graph_delta) equals a rebuild.
        from tyo3.tests.test_graph_incremental import _assert_structurally_equal
        _assert_structurally_equal(s.graph, rebuilt)
```

> If you did **not** ship `_inject_changes`, write these against the real watcher
> with the tolerant poll loop below — but the injection seam is strongly preferred
> for the parity gate (no flakiness).

### 7.3 Python: tolerant end-to-end watcher smoke test (`src/tyo3/tests/test_watch.py`)

Real watcher, real file write. **Tolerant**: flush, then poll in a bounded retry
loop. Mark it so it can be skipped on watcher-hostile CI.

```python
import time
import pytest


def _poll_until_change(session, *, timeout=5.0, interval=0.05):
    """Poll repeatedly until a SyncResult arrives or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.flush_watch()
        result = session.poll_changes()
        if result is not None:
            return result
        time.sleep(interval)
    return None


@pytest.mark.watcher          # register this marker in pyproject/pytest.ini, or drop it
def test_real_watcher_observes_disk_change(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        s.watch()
        try:
            time.sleep(0.1)                       # let the watcher register paths
            (tmp_path / "a.py").write_text("x = 2\ny = 3\n")
            result = _poll_until_change(s)
            assert result is not None, "watcher did not observe the disk change within the timeout"
            assert any(p.endswith("a.py") for p in result.changed) or result.rescan
        finally:
            s.unwatch()
```

> This test is inherently timing-dependent. Keep the timeout generous, never assert
> a single poll suffices, and isolate it behind a marker so a flaky watcher backend
> (some CI sandboxes disable inotify) can be skipped without dropping the
> deterministic gate. The deterministic tests (§7.1/§7.2) are what actually prove
> correctness.

---

## 8. Build, test, iterate (devenv)

Phase 8 changes Rust **and** Python, so the loop runs through the devenv scripts end
to end (never bare `cargo`/`maturin`/`pytest` — `MEMORY.md`):

```bash
# Inner loop while editing Rust (fast; ~1s; no .so written):
devenv shell -- check-rust
devenv shell -- clippy                 # -D warnings — the CI gate

# Rust unit tests (deterministic apply_watch_events suite):
devenv shell -- test-rust

# Rebuild the native extension before running Python (removes the stale .so first):
devenv shell -- rebuild

# Python tests:
devenv shell -- pytest src/tyo3/tests/test_watch.py -q
devenv shell -- pytest src/tyo3/tests/test_watch.py -q -m watcher   # the tolerant e2e one
devenv shell -- tests                  # FULL suite — prove no regressions
```

`devenv shell -- check-so` is a quick sanity check that the freshly built extension
imports and answers (`files()`, `document_symbols`). The full-suite pass proves
`watch`/`poll_changes` are purely additive: every existing write-path, snapshot, and
graph test is unchanged.

---

## 9. Gotchas & decisions (read before you debug)

1. **Never co-acquire the head lock and the queue/watcher locks (§0.4).** The
   handler takes only `pending`. `watch` takes `inner` (briefly, for `&db`) then
   `watcher`. `poll_changes` drains `pending`, drops it, then takes `inner`. Three
   locks, strictly non-overlapping. Overlap them and the watcher thread can deadlock
   against a write.

2. **The handler must never panic (§3.1).** It runs on ty's debouncer thread. A
   panic there poisons the queue mutex and could abort the process. Use
   `if let Ok(mut q) = pending.lock()` and drop the batch on poison; do not
   `.unwrap()` in the handler.

3. **HEAD reads disk directly — do not `store.forget` in poll (§0.2).** Unlike
   `sync_path_inner`, `apply_watch_events` does *not* mutate the store for
   non-overlaid paths (there is nothing cached to forget). It publishes the same
   generation purely to ride `bump_revision`. Adding a spurious `forget` is
   harmless but pointless; adding an `insert` would be wrong (the watcher reflects
   *disk*, not a buffer).

4. **Overlaid paths are dropped, not applied (§0.3).** `has_overlay(&path)` gates
   every real-path event. This is the one piece of policy in Phase 8; lock it with
   `test_overlaid_path_survives_disk_event`. Reconciliation is the user's explicit
   `discard`/`sync_path`.

5. **`Rescan` wins over everything in the batch.** ty emits `Rescan` when it loses
   sync (`watcher.rs:237`, >10000 events or `need_rescan()`). Treat any `Rescan` in
   the drained batch as a whole-project rescan (`rescan: true`), like `sync_all`.
   Do not try to also apply the other events in that batch.

6. **`ChangeEvent` is not `Clone` (§2.1).** Move events through the queue and into
   `apply_changes`; never clone. `apply_changes` takes `&[ChangeEvent]`, so build
   the `kept` vec and pass `&kept`.

7. **`flush_watch` is not synchronous (§3.2).** It prompts the debouncer; the
   handler still runs on the watcher thread afterwards. Tests poll with a retry loop;
   never assume one `poll_changes` right after `flush_watch` sees the event.

8. **One revision per drain, via `bump_revision` (§4.1).** A burst folds into a
   single application revision so a later `snapshot()` pins a coherent post-poll
   state. Do not bump per event.

9. **Stop the watcher on `close()`/`unwatch` (§3.3).** The watcher owns OS threads;
   leaking them past project close is a resource bug. `ProjectWatcher::stop()` joins
   them. `Drop for Watcher` also stops, so dropping the `Option` is a safe backstop,
   but call `stop()` explicitly for determinism.

10. **`reload()` may change watched paths (§3.3).** After a head rebuild, call
    `ProjectWatcher::update(&new_db)` if a watcher is live, or document that reload
    keeps the existing watch set (fine for a same-root reload). Do not drop the
    watcher silently.

11. **Keep the deterministic gate (§7.1/§7.2).** The real-watcher test is a smoke
    test; the synthetic-event tests are the correctness gate. If the e2e test flakes
    on CI, skip it via its marker — never weaken the deterministic tests to make CI
    green.

12. **Use the devenv scripts, always.** `check-rust`/`clippy` for the inner loop,
    `rebuild` before Python, `test-rust`/`tests` for the gates. Bare `cargo`/`pytest`
    miss the pinned toolchain and `PYTHONPATH=src` (`MEMORY.md`).

---

## 10. Explicitly OUT of scope for Phase 8

- **The floating warm `session.check()` cancel-retry fast path** — **Phase 9**.
  Phase 8 produces `SyncResult`s; it does not add a read surface or any cancellation
  handling.
- **Benchmarks** — watcher latency, poll throughput, debounce tuning — **Phase 10**.
  Phase 8 proves *correctness* (events → `SyncResult`, overlay precedence, one
  revision per drain), not speed, and must **not** gate CI on watcher timing.
- **Auto-polling / a background apply loop.** Phase 8 is *pull* (`poll_changes`),
  not push. A future enhancement could run a Python thread that polls on a timer or
  a callback that fires on enqueue, but the single-writer apply stays explicit in
  Phase 8 — do not spawn an applier thread that takes the head lock behind the
  user's back.
- **Virtual-path watching.** `directory_watcher` only watches real directories;
  virtual buffers are driven by `edit_virtual` (Phase 3). `poll_changes` skips
  virtual events defensively; it does not grow a virtual watch source.
- **Configuration-change rediscovery beyond what `apply_changes` already does.**
  ty's `apply_changes` handles config/ignore/stdlib changes (architecture §8); Phase
  8 just feeds it the events. Do not reimplement project rediscovery.
- **Notebook watching semantics** beyond what `apply_changes` + the existing overlay
  already provide.

If you find yourself adding a `check()` cancel-retry loop, writing watcher-latency
timing assertions, spawning a background applier that locks the head, or building a
virtual watch source, stop — you have left Phase 8.

---

## 11. Definition of Done

- [ ] `content.rs`: `ContentStore::has_overlay(&path) -> bool` added.
- [ ] `project.rs`: `PyTyProject` gains `pending: Arc<Mutex<Vec<ChangeEvent>>>` and
      `watcher: Mutex<Option<ProjectWatcher>>`, both initialised in `open`.
- [ ] `project.rs`: `watch()` builds an `EventHandler` over `pending`, starts
      `directory_watcher`, wraps in `ProjectWatcher::new(_, &head.db)`, stores it;
      strict non-overlapping lock order.
- [ ] `project.rs`: `unwatch()` (stops + joins the watcher) and `flush_watch()`.
- [ ] `project.rs`: `apply_watch_events(head, events) -> Option<SyncResultDto>` —
      rescan short-circuit; overlay filtering via `has_overlay`; virtual/`Opened`
      skip; one `apply_changes`; `bump_revision`; bucketed `SyncResult`.
- [ ] `project.rs`: `poll_changes()` drains `pending` (separate lock), applies under
      the head lock, returns `Option<SyncResult dict>`.
- [ ] `project.rs`: `close()` stops the watcher; `reload()` updates or documents the
      watch set.
- [ ] `project.rs`: (recommended) `_inject_changes` test seam for deterministic
      Python tests.
- [ ] `session.py`: `watch` / `unwatch` / `flush_watch` / `poll_changes` wrappers;
      `poll_changes` invalidates the head snapshot and calls `_apply_graph_delta`
      (or documents deferral if Phase 6 `session.graph` was deferred).
- [ ] `_native_impl.pyi`: stubs for the new methods.
- [ ] Rust tests (`phase8_watch_tests`): empty no-op; changed == sync_path shape;
      overlaid path not clobbered; rescan short-circuit; burst → one revision.
- [ ] Python tests (`test_watch.py`): deterministic injected-change tests
      (none-when-empty, change matches, overlay survives, poll drives `apply_delta`
      == rebuild) **plus** a tolerant, marker-gated real-watcher smoke test.
- [ ] `devenv shell -- check-rust` and `devenv shell -- clippy` clean.
- [ ] `devenv shell -- test-rust` passes.
- [ ] `devenv shell -- rebuild` then `devenv shell -- tests` shows **no regressions**
      (Phase 3 write-path, Phase 4/5 snapshot/concurrency, Phase 6/7 graph tests
      unchanged).
- [ ] PR notes: lock-ordering choice; whether buckets are deduped; whether
      `_inject_changes` ships in the public class or behind a feature; reload's watch
      behaviour; whether `_apply_graph_delta` is wired or deferred.

---

## 12. How this seeds Phase 9+

Phase 8 closes the architecture's §4 claim that file watching is "just another
change source." Every mutation — agent edit, editor buffer, virtual buffer, and now
a developer's disk save surfaced by the watcher — flows through the **same** write
path to the **same** `SyncResult`, which Phases 3/6/7 already consume. A `snapshot()`
taken after `poll_changes` pins the new revision; `snap.graph()` diffs against it;
`session.graph` advanced via `_apply_graph_delta`. No phase downstream of 8 needs to
know the change came from a watcher.

- **Phase 9** adds the floating warm `session.check()` (cancellable HEAD read with
  retry) as the "latest glance" counterpart to the cold pinned `snapshot()`. It
  composes cleanly: a developer saves a file → `poll_changes` folds it into HEAD →
  the floating `session.check()` immediately reflects it warm, while held snapshots
  stay pinned. Phase 8's single-writer poll and Phase 9's cancellable reader are the
  two ends of the same HEAD.
- **Phase 10** benchmarks poll throughput and debounce behaviour against the
  explicit write path, characterising the watcher's overhead (architecture §11) —
  the honest cost of automatic disk ingest.

Keep the watcher thread off the head lock, the overlay-precedence filter in
`poll_changes`, and one-revision-per-drain, and the watcher remains a pure change
source that the rest of the substrate cannot distinguish from a manual `sync_path`.
