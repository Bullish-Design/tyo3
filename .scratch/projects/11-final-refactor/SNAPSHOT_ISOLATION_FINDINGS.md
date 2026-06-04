# Snapshot Isolation — Root-Cause Findings & Options

**Trigger:** `TestMultiSnapshotIsolation::test_snapshots_pinned_across_reloads`
fails — a snapshot taken at revision 0 (`snap_a`) sees symbols added in later
revisions. The intern correctly stopped and reported.

**Verdict:** Real gap between the documented contract and actual behavior. The
intern's *assertion* is right; their *diagnosis* ("snapshots reference the same
database") is wrong. The databases are independent. The leak is **lazy file reads
in salsa**.

Reproduction: `.scratch/snapshot_probe.py` (run via `devenv shell -- pyrun`).

---

## 1. What the probe proved

| Scenario | `reload()`? | snapshot read **before** the disk edit? | Snapshot sees the edit? |
|---|---|---|---|
| A | no | no | **Yes (leaks)** |
| B | yes | no | **Yes (leaks)** |
| C | no | **yes** | No (pinned) |
| D | yes | **yes** | No (pinned) |

Two conclusions, both load-bearing:

1. **`reload()` is irrelevant** (A==B, C==D). The snapshot's database is genuinely
   independent — once it has read a file it stays pinned *even after the session
   reloads with new disk content* (C/D). If snapshots shared a database, C/D would
   leak. They don't. **The intern's "same database" theory is disproven.**
2. **The only variable that matters is whether the snapshot materialized the read
   before the disk changed.** That is a lazy-read signature, not a clone bug.

---

## 2. Why — the salsa file-content model (verified in ruff source)

Source: `ruff` rev `3cb09eb`, crates `ruff_db` / `ty_project` / `ty_server`.

- **`source_text(db, file)` is a memoized salsa query** (`ruff_db/src/source.rs:15`).
  It returns `file.source_text_override` if present, else `file.read_to_string(db)`.
- **`read_to_string` reads live disk** via `db.system().read_to_string(path)` and adds
  a salsa dependency on `file.revision(db)` (`ruff_db/src/files.rs:370-388`). So the
  memo is keyed on the file's *revision*.
- **`revision` is only bumped by an explicit `sync`** (`File::sync_*`,
  `set_revision`, needs `&mut Db` — `files.rs:464-491`). Nothing bumps it implicitly.
- ruff documents the consequence verbatim (`files.rs:367`):
  > *"Reading the same file multiple times isn't guaranteed to return the same
  > content. It's possible that the file has been modified in between the reads."*

So content enters a database **lazily, at first `source_text` call**, and is then
frozen until a `sync` bumps the revision. Our wrapper **never syncs** — `reload()`
builds a brand-new `ProjectDatabase` (new storage) and swaps it in.

### The snapshot mechanics, precisely
- `ProjectDatabase: Clone` (`ty_project/src/db.rs:35`) — a clone shares the same
  salsa `Storage` Arc (this is exactly how ty parallelizes: one clone per rayon
  worker, concurrent **reads** on shared storage).
- `snapshot()` clones the live session db. For any file the **parent session has
  already read**, the memo lives in the shared storage → the snapshot inherits it →
  pinned. For any file **not yet read**, the snapshot's first `source_text` reads
  **current disk**.
- When the session later `reload()`s, it replaces its db with a fresh-storage db; the
  old storage is now retained solely by the snapshot → fully decoupled (C/D pinned).

### Why the *existing* `TestSnapshotIsolation` passes (false confidence)
It calls `snap.document_symbols("main.py")` to compute `pre_names` **before** editing
(`test_concurrency.py:290`). That materializes the memo — scenario C/D — so it pins.
Delete that pre-read and it fails identically. Our single-reload test was green for
the wrong reason; the intern's multi-snapshot test is what surfaced the real
semantics.

### Two *separate* invariants (don't conflate them)
- **swap-don't-mutate** (the refactor's §0 invariant): protects *already-materialized*
  snapshot memos from `reload`. ✅ Working — proven by C/D.
- **lazy-first-read**: un-materialized files read live disk on first touch. ❌ The bug.
  `reload()`'s full-rebuild (rather than in-place `sync`) is actually what *preserves*
  the first invariant — so we must not "fix" reload by switching to incremental sync
  without accounting for this.

---

## 3. How ty's own LSP server avoids this (the reference design)

ty never lets salsa race the disk. Its server (`ty_server`):
- Uses a **custom overlay `System`** (`ty_server/src/system.rs`) that wraps an
  `Arc<Index>` of **open in-memory documents**; `read_to_string` serves buffer
  content for open files and only falls back to disk otherwise. The editor buffer —
  not the disk — is authoritative.
- Applies edits through **`&mut db` + sync**, relying on **salsa cancellation** to
  guarantee a single live handle before mutating (`session.rs:131-136`:
  *"we use Salsa's cancellation to guarantee that there's only a single reference to
  the Arc"*).
- Hands each request a **db clone** for GIL-free/parallel reads — the same
  clone-for-reads pattern we use.

We use the raw `OsSystem` (disk) + full-rebuild `reload`, so we inherit the
disk-race semantics ty's overlay was built to avoid.

---

## 4. Options forward

### Option 1 — Eager materialize at `snapshot()` (read-only) ⭐ recommended
At snapshot creation, force-read every project file's `source_text` into the clone's
shared memo before returning:
```rust
fn snapshot(&self) -> PyResult<PySnapshot> {
    let state = clone_locked_state(&self.inner, "snapshot")?;
    for f in state.db.project().files(&state.db).iter() {
        let _ = source_text(&state.db, *f);   // read-only: populates the memo
    }
    Ok(PySnapshot { inner: Mutex::new(Some(state)) })
}
```
- **Pros:** ~10 LOC. **Read-only** — no `&mut`, no salsa cancellation, safe with the
  shared-storage model. Makes the documented §8.4 guarantee true for every file that
  exists at snapshot time. The intern's test passes as written. Cheap when the
  session is "warm" (already `check()`ed → all memos hit, O(1) each).
- **Cons:** `snapshot()` is no longer strictly O(1) — it's O(files) **reads** (no
  semantic analysis) on a cold session. Loses the "free snapshot" framing in §2.3.
  Residual edge: a file created *after* snapshot and then queried by path isn't in the
  captured index and could resolve against disk (document as out-of-scope).
- **Implications:** Snapshot cost becomes "parse all files once" worst-case — still
  far cheaper than `check()`. The shared-storage coupling with the session is
  unchanged (reads only).
- **Opportunities:** Could materialize **lazily-but-pinned** later (capture-on-first-
  read into an override) if cold-snapshot cost ever matters; Option 1 is forward-
  compatible with that.

### Option 2 — `set_source_text_override` on the clone ❌ rejected
Freeze content by setting each file's in-memory override on the snapshot clone.
- **Why rejected:** `set_source_text_override` needs `&mut Db`. The clone **shares
  storage with the live session** until the next reload, so mutating it triggers
  salsa **cancellation** of the session's in-flight reads and bumps the *shared*
  revision — corrupting isolation for other snapshots and forcing session recompute.
  Overrides are only safe on an **independent** db → that's Option 3, not this.

### Option 3 — Independent frozen database per snapshot (maximal correctness)
Give the snapshot its **own** storage + a frozen content source, so it shares nothing
with the session. Two flavors: (3a) build an `InMemorySystem` from a one-time capture
of the project files and construct a fresh db on it; (3b) fresh db + `set_*_override`
per file (now safe, because storage is private).
- **Pros:** **Total** isolation — even files never touched, even files later created/
  deleted on disk. **Zero concurrency coupling** with the session (no shared-storage
  cancellation cross-talk — a strictly cleaner concurrency story than today). Reads
  off the snapshot are pure in-memory.
- **Cons:** Most work. `snapshot()` becomes O(files) read **plus a cold salsa
  storage** (first reads on the snapshot reparse, since memos aren't shared from the
  warm session). Heaviest take cost of all options. New `System` plumbing.
- **Implications:** Decouples snapshot lifetime from session storage entirely — could
  simplify reasoning about `close`/drop ordering. But it's a real subsystem, not a
  patch.
- **Opportunities:** Natural stepping stone to Option 6; the `InMemorySystem` capture
  is reusable for "analyze an in-memory edit without touching disk."

### Option 4 — Relax the contract + document + fix tests (cheapest)
Accept lazy semantics: a snapshot pins the **salsa revision** and **already-read**
content, and observes disk edits lazily until a file is first read. Update §8.4 /
README, and change the multi-snapshot test to **materialize each snapshot before
editing** (read-then-pin, like the existing single-reload test).
- **Pros:** Zero implementation change; `snapshot()` stays O(1). Honest about what the
  current mechanism delivers. Fine for the realistic flow *open → check (materializes
  everything) → snapshot → reads*, where everything is already pinned.
- **Cons:** Abandons the headline feature value. "Take a snapshot to isolate from a
  concurrent edit" — the §8.4 reason to exist — is **no longer guaranteed** unless the
  caller pre-reads every file they'll touch. Subtle, easy to misuse. Two of our tests
  would have to assert the weaker property.
- **Implications:** Snapshot becomes "revision-consistency across reads on a stable
  disk" + GIL, not "edit isolation." Still useful (consistent multi-read within one
  operation) but a smaller promise.
- **Opportunities:** Could be paired with a documented `snapshot.materialize()` helper
  so callers opt into pinning when they need it — middle ground between 4 and 1.

### Option 6 — Adopt ty's overlay-System + incremental-sync architecture (strategic)
Replace `OsSystem` + full-rebuild `reload` with an overlay `System` over an in-memory
document store, and apply edits via `&mut db` + `sync` under salsa cancellation —
mirroring `ty_server`.
- **Pros:** The "correct" long-term model; content is controlled, not raced. O(1)
  snapshots that are genuinely consistent. Incremental reload (faster than full
  rebuild). Aligns us with upstream ty if TyO3 grows toward a real server/LSP.
- **Cons:** Largest change by far; touches the whole session/reload/snapshot surface
  and the concurrency model. Overkill unless edit-isolation/perf at scale is a product
  goal.
- **Implications:** Effectively a re-architecture of the file layer.
- **Opportunities:** Unlocks in-memory editing, virtual files, watch-mode, and a real
  LSP server without further file-layer churn.

---

## 5. Recommendation

**Ship Option 1 now**, fix the tests, and record Option 3/6 as the principled
direction if TyO3 ever needs cold-snapshot isolation or grows into a server.

Rationale: Option 1 is the smallest change that makes the **documented** guarantee
true, it's **read-only** (so it sidesteps the cancellation hazard that sinks Option
2), and it's forward-compatible with the heavier options. The cost — O(files) reads on
a cold snapshot — is acceptable because (a) any session that has run `check()` makes it
O(1), and (b) snapshots exist precisely for "many reads over one revision," so a
one-time capture amortizes well.

### Concrete next steps
1. **Implement Option 1** in `rust/src/project.rs::snapshot()` (add `use
   ruff_db::source::source_text;` — already imported). `clean && build`.
2. **Re-run** the intern's `TestMultiSnapshotIsolation` — expect green **without**
   editing the assertions.
3. **Harden `TestSnapshotIsolation`** so it can't pass for the wrong reason: add a
   variant that takes the snapshot, edits + reloads, and reads the snapshot for the
   **first time** *after* the edit (the scenario-A shape). With Option 1 it must still
   be pinned. This guards the fix permanently.
4. **Update the docs** (`OPTION_C…md` §8.4 note + README Concurrency section): state
   that `snapshot()` captures file content at creation (cost + guarantee), and that
   isolation is therefore total for files present at snapshot time.
5. **Record in repo memory:** snapshot pins content by materializing `source_text` at
   creation; the lazy-read model + ty's overlay reference; that `reload`'s full rebuild
   is what protects already-materialized memos (don't switch to in-place sync without
   revisiting this).

> Whichever option is chosen, **do not** "fix" the failing test by weakening its
> assertion — the assertion encodes the intended contract. Either make the contract
> true (1/3/6) or change the *documented contract and the test together, consciously*
> (4).
