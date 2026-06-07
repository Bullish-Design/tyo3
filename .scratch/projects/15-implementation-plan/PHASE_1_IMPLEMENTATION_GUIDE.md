# Phase 1 — Make Committed Generations Complete (the content gate)

> A step-by-step execution guide for **Phase 1** of
> `REFINED_IMPLEMENTATION_PLAN.md`. Read that plan's "Phase 1" section and §5.1 /
> §5.2 of `REFINED_IMPLEMENTATION_CONCEPT.md` once before starting — this guide
> assumes that vocabulary (Revision, Generation, ContentStore, Document, frozen
> overlay) and turns it into concrete edits against the code as it exists today.

---

## 0. The dev environment — read this first, it governs every command below

This repository is a **devenv.sh (Nix-managed) dev environment**. The toolchain
(the right `rustc`, `cargo`, `maturin`, `python`, `pytest`, `ruff`, and the
project's custom scripts) only exists *inside* the devenv shell. A `cargo` or
`pytest` you run from a bare login shell is either missing or the wrong version,
and will give you misleading results.

**The rule for the entire phase: every in-repo operation runs through the devenv
shell.** Two equivalent forms:

- One-shot (preferred in this guide, copy-pasteable):
  ```bash
  devenv shell -- cargo test --manifest-path rust/Cargo.toml content
  ```
- Interactive (if you are iterating rapidly):
  ```bash
  devenv shell        # drop into the environment once
  # …then run cargo / pytest / the project scripts directly inside it…
  ```

The project also defines custom scripts inside `devenv.nix` — `build`, `tests`,
`test-rust`, `test-quick`, `clean`, `status`, etc. When this guide says "rebuild
the extension," it means `devenv shell -- build` (which runs maturin so Python
sees the freshly compiled Rust). **A pure `cargo test` does not refresh the
compiled extension Python imports** — after any Rust change that Python must
observe, run `devenv shell -- build` before the pytest gate.

The suites are slow. Allow **~10–15 minutes** for a full run; launch full suites
in the background and give them time rather than assuming a hang.

> Throughout the rest of this document, **assume every `cargo`, `pytest`,
> `maturin`, `ruff`, and project-script invocation is prefixed with
> `devenv shell --`**, even where a line is abbreviated for readability.

---

## 1. What Phase 1 changes, and why

**Goal (from the plan):** committed generations become the *complete,
authoritative* record of every revision, so snapshots never read disk (§5.1,
§5.2).

**The defect today** (confirmed in the current code):

- The live head `ContentStore` is **empty at open**. Real file content is never
  interned into a generation; instead the *live* `OverlaySystem` falls through to
  the native `OsSystem` on a miss (`overlay.rs:181`, `:197`), and ty lazily reads
  disk. `sync_all` just republishes the (empty) capture and issues a `Rescan`
  (`project.rs:2037-2039`).
- Because generations are empty, snapshot construction has to **re-walk the live
  filesystem** to become usable: `build_frozen` calls `pre_populate_generation`
  (`project.rs:1122`), which runs an `OsSystem` walk and reads each relevant file
  off disk *at snapshot time* (`project.rs:1067-1112`).

That makes snapshot capture O(files) and — worse — lets a snapshot pinned at R
observe content that was written to disk *after* R but read during construction.
That is the §5.1/§5.2 violation Phase 1 closes.

**The fix:** read disk **once, at commit time** (and at open), interning every
relevant file into the revision-owned generation. Then a frozen snapshot is built
directly from an already-complete captured generation, with the frozen overlay
strictly forbidden from touching disk.

### Files in scope
- `rust/src/content.rs` — the `ContentStore` / `Generation` (add ingest helpers).
- `rust/src/project.rs` — open path, `build_frozen`, delete
  `pre_populate_generation`, the disk-read counter seam.
- `rust/src/overlay.rs` — verify/lock down the frozen strictness (mostly already
  correct).
- `src/tyo3/tests/test_final_content_spine.py` — the Phase 0 invariant test that
  Phase 1 must turn green (already written; do **not** weaken it).

---

## 2. Working rules for this phase (non-negotiable)

1. **Write/await the failing test first, then implement until green.** Phase 0
   already shipped `test_final_content_spine.py` with one `xfail(strict=True)`
   (the disk-read counter seam) and several live assertions. Your job is to make
   the seam exist and all five behaviours hold — which will flip the strict-xfail
   to a *failure* and force you to remove the marker.
2. **Never weaken a test to make progress.** If an assertion is hard to satisfy,
   the implementation is wrong, not the test.
3. **Don't swallow errors.** A disk read that fails during ingest is a real
   condition — surface it, don't silently skip (except the deliberate, commented
   "absent path → tombstone" case).
4. **Milestone gate after the phase** (both must pass, both via devenv):
   ```bash
   devenv shell -- pytest -q --no-cov
   devenv shell -- cargo test --manifest-path rust/Cargo.toml
   ```

---

## 3. Pre-flight — establish the red baseline

Confirm the starting state before changing anything, so you can prove your work
moved the needle.

```bash
# 1. The Rust core compiles and its content/overlay/project tests pass today.
devenv shell -- cargo test --manifest-path rust/Cargo.toml content overlay project

# 2. The Phase 0 invariant test is RED in exactly the expected way:
#    - test_snapshot_construction_reads_no_disk → xfailed (seam absent)
#    - the other four → currently pass OR fail depending on disk timing;
#      note which, so you can confirm they are solid green at the end.
devenv shell -- pytest src/tyo3/tests/test_final_content_spine.py -q --no-cov -rA
```

Record the output. The strict-xfail on `test_snapshot_construction_reads_no_disk`
is your North Star: it exists precisely because the
`project_content_disk_reads()` seam does not exist yet.

---

## 4. Step-by-step implementation

Do the steps in order. Each step lists **what**, **why**, **where**, and a
**verify** command (devenv-prefixed). Commit at the natural breakpoints noted.

### Step 1.1 — Make "project-relevant content" a single authority

**What.** There is already a predicate, `snapshot_relevant_file`
(`project.rs:1053-1065`): a path is relevant if it has a Python source extension
*or* its file name is one of `pyproject.toml`, `ty.toml`, `setup.cfg`,
`setup.py`. Promote this to *the one* authority for "what belongs in a
generation," used by both ingest (Step 1.2/1.3) and any future caller.

**Why.** §5.1 requires one definition of relevant content. Two definitions drift;
a file that ingest includes but the predicate excludes (or vice versa) produces
either phantom or missing entries in a generation.

**Where / how.**
- Rename it to make its new, broader role explicit (e.g.
  `is_project_relevant(path: &SystemPath) -> bool`) and move it somewhere both
  `content.rs`'s ingest and `project.rs` can call it. A small free function in
  `content.rs` (or a tiny `relevance.rs` module re-exported from both) is fine.
- **Explicitly exclude the TyO3 sidecar's own `.tyo3/config.toml`.** The sidecar
  config is *not* analysed content and must never enter the analysis generation.
  Today the extension check would not match `config.toml` (its name is not in the
  allow-list), but the walk could descend into `.tyo3/` and pick up Python files
  written there. Add a guard that rejects any path under the `.tyo3/` sidecar
  directory outright, and add a unit test asserting `.tyo3/config.toml` and
  `.tyo3/anything.py` are *not* relevant.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml relevant
```

> Commit here: `refactor(content): single is_project_relevant authority; exclude sidecar`.

---

### Step 1.2 — Add disk ingest to the content store

**What.** Add native helpers on `ContentStore` (in `content.rs`) so disk is read
**once, at commit time**, into the revision-owned generation. The plan names
three:

- `ingest_project(root, filter) -> Revision` — walk `root` once, and for **every
  relevant file**, read it once and intern a `Document::Text` (which already
  computes and carries its `ContentHash` at construction, `content.rs:45-49`). A
  relevant path that the walk shows as absent becomes a `Document::Deleted`
  tombstone. This advances the revision exactly once (one walk = one batch = one
  revision).
- `apply_disk_batch(paths) -> Revision` — re-read a specific set of disk paths
  once and intern them as one batch (one revision). This is what `sync_path` and
  the watcher's `poll_changes` will eventually feed (Phase 5 funnels them; Phase 1
  only needs the store-level primitive to exist and be correct).
- `apply_overlay_batch(changes) -> Revision` — already essentially present as
  `apply_batch` (`content.rs:203`). Either alias it or rename for symmetry; the
  point is one batch = exactly one revision, which `apply_batch` already
  guarantees and tests (`apply_batch_is_atomic_one_revision`,
  `content.rs:371`).

**Why.** §5.1: "Every revision-producing event MUST write its resulting content
into the generation for that revision *before* the revision is published … an
ingested disk change reads disk **once**, at commit time … Ingest MUST NOT defer
the disk read to snapshot time."

**Where / how — important details.**
- **Version numbers.** `Document::text` needs a `version` from the store's
  monotonic counter (`content.rs:45`, `:176-190`). Route ingest through the same
  `mutate`/`apply_batch` machinery so interned documents get real version numbers
  — **do not** repeat the `pre_populate_generation` shortcut of stamping
  `version = 0` (`project.rs:1105`), which would make metadata revisions collide.
- **The walk.** Reuse the `OsSystem::walk_directory` pattern already in
  `pre_populate_generation` (`project.rs:1076-1098`) — but it now lives in the
  store-side ingest path and runs at commit/open time, not snapshot time.
- **Disk-read counting (the seam).** Increment a counter on *every* project-
  content disk read inside ingest (see Step "Test seam" below). This is the one
  observable that the Phase 0 test asserts against.
- **Errors.** If `read_to_string` fails for a path the walk reported as a file,
  that is not "absent" — propagate it (or log with context and skip with a
  one-line comment justifying it). Reserve tombstones for genuinely-absent
  relevant paths.
- **Determinism / ordering.** The frozen overlay enumerates directory membership
  from generation keys in sorted (`BTreeMap`/`BTreeSet`) order
  (`overlay.rs:285`), which differs from native `readdir` order. Ingesting the
  full set up front is what makes that ordering authoritative and stable — good,
  but be aware order-sensitive downstream tests may shift. (See the project memory
  note "Frozen walk sorted ordering.")

**Verify.** Write store-level unit tests in `content.rs`'s `#[cfg(test)] mod
tests` (alongside the existing ones):
- ingest of a temp dir with `a.py` + `pkg/m.py` + `pyproject.toml` interns all
  three, each as `Document::Text` with a non-zero version and a content hash;
- ingest advances the revision exactly once;
- an absent-but-relevant path produces a tombstone, not a panic.

```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml content
```

---

### Step "Test seam" — the project-content disk-read counter

**What.** Add an atomic counter that increments on every project-content disk read
and expose it to Python on the native handle.

**Why.** `test_snapshot_construction_reads_no_disk` proves "snapshot construction
reads no disk" *structurally* (not by timing). It looks for one of these methods
on `session._inner`: `project_content_disk_reads`, `_project_content_disk_reads`,
or `disk_read_count` (`test_final_content_spine.py:119`).

**Where / how.**
- Add an `Arc<AtomicU64>` that the ingest path bumps once per file actually read
  from disk. Natural home: a field threaded from `PyTyProject` into the ingest
  call, or a counter on `ContentStore` itself with a getter. Keep it simple and
  global-to-the-project.
- Expose a `#[pyo3] fn project_content_disk_reads(&self) -> u64` on `PyTyProject`
  (the `TyProject` pyclass, `project.rs:162`). It loads the atomic.
- After Phase 1, `snapshot()` performs **no** ingest, so calling `snapshot()` /
  `snap.files()` leaves the counter unchanged — which is exactly what the test
  asserts (`before == after`).

**Verify.** This is also Step 1.4's payoff; the test goes green only once
`pre_populate_generation` is gone. After this step alone the seam *exists* (the
`xfail` will start *failing* because the method is now present), which is the
signal to remove the `@pytest.mark.xfail(...)` decorator
(`test_final_content_spine.py:126-130`).

---

### Step 1.3 — Seed content when the project opens

**What.** On `open` (`project.rs:1680`), after loading+validating config and
creating the content store, **ingest the project into the initial revision**
*before* building the live database and reconciling identity. Sequence:

```
load + validate config
  → create ContentStore (with retain_cap)
  → ingest_project(root, is_project_relevant)        # NEW: disk read once
  → build the live ProjectDatabase over that generation
  → reconcile identity against it                     # populate the registry now
```

**Why.**
- §5.1: opening must fully populate the generation so no *later read* needs to
  trigger a *write* to populate it.
- Phase 4 depends on this: identity must be reconciled at open so a later graph
  read never has to call `sync_all`. Phase 1 lays the groundwork by making the
  generation complete and reconciling once at open.

**Where / how.**
- `build_head_with_config` (`project.rs:955`) currently builds the db over the
  *empty* `initial_store.capture()` (`project.rs:966`). Change the open path so
  the store is ingested *before* the capture used to build the db — either ingest
  inside `build_head_with_config` right after constructing `system`, or ingest the
  store in `open` and pass the populated store in. Prefer ingesting in one place
  the live db is guaranteed to read the populated generation.
- After the db is built, run the existing identity reconciliation against it (the
  helper around `run_identity_reconciliation` / `reconcile`, `project.rs:1366+`,
  `:2042`) so the registry is populated at open.

**Document the initial revision.** Decide and **write down** whether the seeded
initial state is revision 0 or revision 1, then make a test assert it. Today the
store seeds `Revision(0)` empty (`content.rs:147-154`); if `ingest_project` runs
as the first batch it advances to `Revision(1)`. Pick one convention, document it
in a doc-comment on `open`, and assert `session.head` equals it in a test.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml project
# Behavioural: a freshly-opened session sees its files without an explicit sync.
devenv shell -- pytest src/tyo3/tests/test_final_content_spine.py -q --no-cov -rA
```

---

### Step 1.4 — Delete snapshot disk pre-population

**What.** Remove `pre_populate_generation` (`project.rs:1067-1112`) entirely, and
its call inside `build_frozen` (`project.rs:1122`). `build_frozen` now receives an
**already-complete** generation (because the head store was ingested at open and
re-ingested at each commit) and builds the frozen overlay directly from it.

**Why.** This is the actual O(1)-capture fix and the thing that makes
`test_snapshot_construction_reads_no_disk` pass. As long as
`pre_populate_generation` exists and runs at snapshot time, the counter increments
during `snapshot()` and the test fails.

**Where / how.**
- In `build_frozen` (`project.rs:1114-1168`), replace
  ```rust
  let gen_full = pre_populate_generation(&root, &generation);
  let system = OverlaySystem::frozen(root.clone(), gen_full, rev);
  ```
  with
  ```rust
  let system = OverlaySystem::frozen(root.clone(), generation, rev);
  ```
- Delete the `pre_populate_generation` function and any now-unused imports
  (`OsSystem`, the walk types, `Mutex` if it was only used there). The Step 12
  clippy gate will later catch stragglers, but clean up as you go.
- `snapshot` (`project.rs:2213-2249`) already captures the generation under the
  lock and drops the lock *before* `build_frozen` (`project.rs:2238`). With
  pre-population gone, the work after the lock is just the (cold) db build over a
  complete, pinned generation — no disk content read.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml overlay project
devenv shell -- build       # refresh the compiled extension for pytest
devenv shell -- pytest src/tyo3/tests/test_final_content_spine.py -q --no-cov -rA
```
At this point **remove the `@pytest.mark.xfail` marker** from
`test_snapshot_construction_reads_no_disk` (it must now pass on its own).

---

### Step 1.5 — Keep the frozen overlay strict

**What.** Confirm — and lock in with a test — that the frozen overlay never falls
through to disk on a miss; a miss is "absent at R." It *may* synthesise directory
membership from the generation's keys (that derives membership from the
generation, not disk).

**Why.** §5.2: "A snapshot reads through a frozen overlay: a miss is 'absent at
R,' never a live-disk read."

**Where / how — mostly verification, the code is already correct.**
- `read_to_string` returns `not_found` on a frozen miss (`overlay.rs:195`). ✓
- `path_metadata` on a frozen miss returns a synthesised directory only if a
  generation key lives under it, else `not_found` (`overlay.rs:169-179`). ✓
- `source_type` returns `None` on a frozen miss (`overlay.rs:247`). ✓
- `read_directory` enumerates from generation keys, skipping tombstones
  (`overlay.rs:280-330`). ✓
- **`walk_directory` is the gap to watch** (`overlay.rs:332-343`): it currently
  delegates to native disk even for frozen views (a documented ruff_db
  constructor limitation). The module comment argues this is safe because any
  walked file not in the generation fails on `read_to_string`. With generations
  now complete, re-examine whether any Phase 1 read path reaches `walk_directory`
  on a frozen view and could observe a post-R disk file. If a frozen
  `walk_directory` can leak a newer disk entry, that is a real §5.2 hole — note
  it, and either gate it behind the generation keys or open a follow-up. The
  existing overlay tests `new_disk_file_absent_from_frozen_present_in_live`
  (`overlay.rs:588`) and `frozen_view_never_reads_disk_for_content`
  (`overlay.rs:477`) cover `read_directory`/`read_to_string`; consider adding the
  `files()`-path equivalent at the Python level.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml overlay
```

---

## 5. Acceptance — the Phase 1 gate

Run exactly what the plan's Phase 1 "Acceptance" lists, all via devenv:

```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml content overlay project

devenv shell -- build   # ensure pytest imports the freshly built extension

devenv shell -- pytest \
  src/tyo3/tests/test_final_content_spine.py \
  src/tyo3/tests/test_mvcc_snapshots.py \
  src/tyo3/tests/test_mvcc_concurrency.py \
  -q --no-cov
```

Then the full milestone gate (run the suites in the background; ~10–15 min):

```bash
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria (all must hold):**
- Snapshot construction reads **no** project content from disk — proven by the
  counter seam, with the `xfail` marker removed from
  `test_snapshot_construction_reads_no_disk`.
- The same revision yields identical content and identical directory listings
  regardless of disk churn (tests 1–3 in `test_final_content_spine.py`).
- Snapshot capture is O(1) in content (no walk at snapshot time;
  `pre_populate_generation` is deleted).
- Pinning an evicted revision raises the typed `RevisionEvicted` (test 5 —
  already wired via `snapshot(at=…)` → `RevisionEvictedError`,
  `project.rs:2229`).
- `mvcc_snapshots` and `mvcc_concurrency` stay green (isolation preserved).

---

## 6. Pitfalls specific to this phase

- **Forgetting `devenv shell -- build` before pytest.** A green `cargo test`
  does not update the compiled extension Python imports. If a Python test behaves
  as though your Rust change isn't there, you skipped the rebuild.
- **`version = 0` regression.** Do not copy `pre_populate_generation`'s
  `Document::text(text, 0)` shortcut into ingest. Route through the store counter
  so metadata revisions stay monotonic (`overlay.rs:156-164` derives file
  metadata revision from the document version).
- **Ingesting the sidecar.** Ensure the walk does not descend into `.tyo3/` and
  intern its config or any helper `.py` there (Step 1.1).
- **Revision-number convention.** If you flip the initial revision from 0 to 1,
  audit tests and docs that assume `head == 0` at open. Decide once, document,
  and assert.
- **Borrow-checker friction** moving the walk into `content.rs`: the
  `Arc<Mutex<Vec<…>>>` collect pattern from `pre_populate_generation`
  (`project.rs:1079-1098`) carries over cleanly; keep the counter as a separate
  `Arc<AtomicU64>` rather than entangling it with the path collection.
- **`walk_directory` frozen fallthrough** (Step 1.5) — the one place a frozen
  view can still touch disk. Verify no Phase 1 read path depends on it; if one
  does, treat it as a real isolation hole.

---

## 7. Suggested commit sequence for the phase

1. `refactor(content): single is_project_relevant authority; exclude sidecar` (1.1)
2. `feat(content): disk ingest helpers (ingest_project / apply_disk_batch)` (1.2)
3. `feat(project): project-content disk-read counter seam` (test seam)
4. `refactor(project): ingest project content at open; reconcile identity at open` (1.3)
5. `refactor(content): remove snapshot disk pre-population; O(1) capture` (1.4 — also drops the xfail)
6. `test(overlay): lock in frozen-overlay strictness; no disk fallthrough` (1.5)

This maps to the plan's single commit
`refactor(content): complete generations; remove snapshot disk pre-population`;
squash on landing if the series is preferred as one reviewed commit.

Every commit passes its focused `cargo`/`pytest` slice; the last one passes both
full suites (the milestone gate). Remember: **all of it through
`devenv shell --`.**
