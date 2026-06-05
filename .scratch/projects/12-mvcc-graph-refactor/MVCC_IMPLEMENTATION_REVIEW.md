# TyO3 MVCC Substrate — Consolidated Implementation Review

> Status assessment of the MVCC graph refactor as of 2026-06-05, branch
> `chore/final-refactor`. Synthesises three independent intern reviews against
> `MVCC_SUBSTRATE_ARCHITECTURE.md`, with every load-bearing claim re-verified
> directly against source. Where the reviews disagreed, this document states
> which reading the code supports and why.
>
> **Verification basis:** claims below marked **[verified]** were confirmed by
> reading the cited source in this review. Claims marked **[needs test]** are
> structurally plausible and located in source, but their runtime manifestation
> was not reproduced and should be pinned with a targeted test before acting.

---

## 1. Executive summary

The architecture is sound and the central bet is correct. Salsa's
`cancel_others` (`storage.rs:160`) blocks any `&mut db` mutation until it is the
only live handle, which makes shared-storage MVCC impossible; giving each pinned
revision its own `ProjectDatabase` (its own `Zalsa`) is the only coherent
resolution. `build_frozen` (`project.rs:872`) delivers exactly that, and the
Phase 5 concurrency tests demonstrate the payoff: held snapshots do not block
the writer, and snapshot reads are never cancelled. That foundation should not
be touched.

However, the document's **headline correctness invariant is not yet met**, and
this is the single most important finding. The architecture states (§2.2):

> "a file's content for revision R is fixed the moment it is first interned into
> the store at R. Disk is read at most once per (path, revision)."

As implemented, the read-once guarantee is per **(path, snapshot instance)**,
not per **(path, revision)**. Disk content never enters the `ContentStore`;
each frozen snapshot lazily reads disk into its own private cell. Two snapshots
pinned at the same revision can therefore disagree about a disk-backed file if
disk changed between their creation times. **MVCC isolation is real for
overlaid (agent-edited) content and not yet real for disk-divergence windows.**

The honest framing of the current state:

- **The MVCC engine works for the intended agent workflow** — edit through the
  overlay, then snapshot. Edited files are correctly pinned and isolated.
- **The store is not yet the system of record for disk content**, contrary to
  the doc. Three findings (snapshot disk capture, `sync_path`, watcher) are the
  same root cause.
- **One clear atomicity bug** (`edit_many`) and **one stale-contract hazard**
  (misleading comments) should be fixed regardless of phase.
- **The Rust module organisation has outgrown its file** (`project.rs` is 2972
  lines) and carries real duplication, but this is quality, not correctness.

Overall: architecturally strong (the design is the right shape), implementation
is mid-flight with one genuine invariant gap that the doc currently overstates
as complete.

---

## 2. What is verified working

- **[verified] Per-revision independent storage.** `build_frozen`
  (`project.rs:872`) constructs a fresh `ProjectDatabase` over
  `OverlaySystem::frozen` with its own `Zalsa`. Held snapshots cannot be
  cancelled by, and cannot block, HEAD `apply_changes`. This is the core thesis
  and it holds.
- **[verified] Content store data structures.** `ContentStore` uses
  `rpds::HashTrieMapSync` (`content.rs:46`); `capture()` is an O(1) `Arc::clone`
  (`content.rs:111`); generation publishing is lock-free via `ArcSwap`
  (`overlay.rs:35`). Bounded `retained` buffer (cap 256) supports
  `snapshot(at=r)` time-travel with oldest-eviction (`content.rs:202`).
- **[verified] Overlay shadowing and tombstones.** Text overlays shadow disk,
  `Deleted` tombstones hide disk files, and metadata revision tracks content
  version (`overlay.rs` tests at lines 354–405).
- **[verified] Race-safe lazy capture within a snapshot.**
  `capture_disk_file` (`overlay.rs:116`) uses additive RCU/CAS: concurrent
  readers of one snapshot converge on a single interned `Document`. (Note: this
  is intra-snapshot safety; it does **not** address cross-snapshot consistency —
  see §3.1.)
- **[verified] Watcher overlay-wins semantics.** `apply_watch_events`
  (`project.rs:1087`) drops disk events for paths that have a live overlay
  buffer, so unsaved edits win over racing disk events.
- **[verified] Graph delta pins its source snapshot.** `_apply_graph_delta`
  (`session.py:854`) opens `snapshot(at=result.revision)` and updates the HEAD
  graph from that pinned revision, not from a drifting HEAD.
- **[verified] Multi-file dirty re-index ordering.** `_index_files`
  (`graph.py:1125`) correctly phases: collect symbols → materialize all nodes →
  structural edges → references → inheritance, so two simultaneously-changed
  files that reference each other resolve (every dirty node exists before any
  reference resolves).

---

## 3. Findings (severity-ordered, consolidated)

### 3.1 — HIGH: the store is not the system of record for disk content

**[verified]** This consolidates Intern 1's findings #1 and #2, which share one
root cause and should be treated as one work item.

**Mechanism:**
- `generation_at(R)` (`content.rs:212`) returns the retained `ContentMap` for
  revision R. That map contains **only overlaid paths** — entries created by
  `insert_text` / `insert_virtual` / `delete`. Disk-backed files that were never
  edited are absent.
- `build_frozen` (`project.rs:872`) hands that generation to
  `OverlaySystem::frozen`, which holds its **own private `ArcSwap`**
  (`overlay.rs:67`).
- On a miss, a frozen overlay calls `capture_disk_file` (`overlay.rs:116`),
  which reads disk **at query time** and interns into that snapshot instance's
  cell.

**Consequence:** the read-once guarantee binds to **(path, snapshot instance)**,
not **(path, revision)**. Two snapshots both pinned at revision R, created at
different times, will disagree about a disk-backed file if disk changed in
between. This directly violates the doc's §2.2 invariant.

**Compounding paths (same root cause):**
- `sync_path_inner` (`project.rs:1018`) calls `forget(&abs)` then publishes and
  lets the overlay fall through to disk — it does **not** write the new disk
  content into the store.
- `apply_watch_events` (`project.rs:1108`) publishes the *content-unchanged*
  generation and bumps the revision, with the comment "HEAD reads disk
  directly, so no store mutation is needed." A watcher-produced revision is
  therefore not content-pinned either.
- Directory enumeration: `read_directory` / `walk_directory`
  (`overlay.rs:274,281`) delegate to `native` unconditionally, even when frozen.
  A snapshot's `files()` listing reflects live-disk directory membership, not
  membership at revision R.

**Severity rationale (resolving the Intern 1 vs Intern 2 disagreement):**
Intern 2 argued this is an unreached phase, not a violation, citing the
intra-snapshot race-safety test. That test only proves consistency *within one
snapshot*; it does not address the cross-snapshot-same-revision case, which is
the actual hole. The invariant the doc claims as *already holding* does not
hold. **This is a correctness gap, not a phase footnote — HIGH.**

**Blast radius (where Intern 2 is right):** in the intended workflow (agent
edits via overlay → snapshot), edited files *are* in the store and *are*
correctly pinned. The hole opens only for files that change on disk without
going through `edit()`. So this is High-but-scoped, not "snapshots are broken."

**Fix direction:** route all disk ingest through the retained generation — lazy
capture, `sync_path`, and the watcher should write `Document::Text` / `Deleted`
(including directory membership / tombstones) into the revision-owned generation
shared by all snapshots at R, rather than into per-snapshot private cells.

---

### 3.2 — HIGH: `edit_many` is not atomic

**[verified]** All three perspectives agree; confirmed in source.

`edit_many` (`project.rs:1241`) loops calling `head.store.insert_text(...)`
(`project.rs:1254`) per file. Each `insert_text` calls `mutate` (`content.rs:122`),
which **bumps the revision and calls `record_retained`** every iteration. A
two-file `edit_many({"a": ..., "b": ...})` therefore retains an intermediate
generation containing only `a`, at revision `r0+1`. `commit_head` publishes and
calls `apply_changes` once at the end, so HEAD is fine — but
`snapshot(at=r0+1)` can observe the half-applied batch.

The method's own docstring promises "one publish, one `apply_changes`, one
published revision" — the publish/apply half is honored, the revision-counter
half is not.

**Fix direction:** add a batch mutation API to `ContentStore` that applies all
map inserts inside a single `mutate()` call (one revision bump, one
`record_retained`). `version_counter` may still increment per file; only the
application `Revision` must advance once.

---

### 3.3 — MEDIUM: virtual buffers are write-only through the public read surface

**[verified]** `edit_virtual` (`project.rs:1267`) stores a virtual document via
`insert_virtual`, and the overlay serves it (`read_virtual_path_to_string`,
`overlay.rs:200`). But `compute_files` (`project.rs:192`) walks only
`project.files()` (indexed disk files), and all content reads go through
`resolve_file_and_source` → `file_resolver::resolve_file`, which canonicalizes.
So `files()` omits virtual buffers and `document_symbols("untitled:1")` raises
`PathResolutionError`.

**Severity rationale (resolving Intern 1 vs Intern 2):** Intern 1 rated this
High as a "core capability undercut"; Intern 2 rated it Medium. The code shows a
real public-API gap, but virtual analysis *does* work through ty's virtual-file
machinery — what's missing is a public read surface, not the underlying
capability. **Medium**, with the caveat that raising `PathResolutionError` is a
poor failure mode.

**Fix direction:** add an explicit `read_virtual(uri)` read surface rather than
routing virtual URIs through the canonicalizing filesystem path.

---

### 3.4 — MEDIUM: stale contracts in comments and docs

**[verified]** `project.rs:1214-1216` still states a live snapshot "shares the
HEAD Zalsa and will block `apply_changes` forever ... Phase 4 fixes this" — but
Phase 4 is implemented and `build_frozen` gives snapshots their own `Zalsa`. The
README's "mutation swaps the canonical database wholesale" is also wrong; HEAD is
mutated in place. These actively mislead maintainers about the current
concurrency model.

**Fix direction:** correct the write-path comment block and the README
concurrency section to describe HEAD-in-place + independent frozen snapshots.

---

### 3.5 — LOW now / latent HIGH: HEAD-graph mutation is outside the write lock

**[verified structurally]** This is where the Intern 1 (Medium) vs Intern 2
(Low) split resolves to "it depends on the concurrency contract."

Native writes are serialized by `Mutex<HeadState>`, but the Python HEAD-graph
mutation in `_apply_graph_delta` (`session.py:854`) runs **outside** that lock.
Each write method (`edit`, `edit_many`, …) calls the native write, then
separately calls `_apply_graph_delta`.

- Intern 2 is right that in-call corruption is unlikely: the GIL serializes the
  pure-Python graph mutations and each `apply_delta` rebuilds from its own
  pinned snapshot.
- The hazard neither review named precisely: under genuinely concurrent Python
  writers, nothing forces `_apply_graph_delta` calls to run in revision order.
  An older revision's subgraph could be applied after a newer one's, leaving the
  HEAD graph behind a newer revision's content.

Under the architecture's stated **single-writer** model (§7: "One writer. A
`Mutex<Head>` serialises every mutation"), this is a non-issue. But the native
Mutex creates a false impression that "writes are serialized" when the graph
mutation is not covered by it, and the doc's premise ("many concurrent agents")
invites the expansion that would make it a real bug.

**Fix direction:** make a decision and record it. Either (a) document that
mutation is single-writer and reads are the concurrent surface, or (b) wrap
`edit*()` + `_apply_graph_delta()` in a session-level write lock so the graph
update is serialized with the native write.

---

### 3.6 — NEEDS TEST: inheritance OVERRIDES depends on within-pass edge order

**[needs test]** Intern 3 reported this as an incremental-mode correctness bug.
Verification refines it on two points:

1. **It is not incremental-specific.** The structure is identical in the full
   build (`graph.py:209-219`, Pass 5) and the incremental path
   (`graph.py:1169-1178`). Both call `_resolve_inheritance` per file in a loop,
   and within each call they add that file's INHERITS edges *and* compute its
   OVERRIDES via a BFS over the graph's existing INHERITS edges
   (`graph.py:910-935`).

2. **Whether it manifests depends on `class_supertypes` semantics.** The
   OVERRIDES BFS walks multi-level INHERITS chains in the graph. If `A→B→C` and
   all three are processed in the same pass, processing `A` first adds `A→B` but
   `B→C` does not exist yet (B is processed later in dict order), so `C`'s
   methods are missed for `A`'s overrides. This is only a real bug if
   `class_supertypes` returns **direct** supertypes (requiring the graph BFS to
   recover transitivity). The presence of the BFS strongly implies direct-only
   supertypes — otherwise the multi-level walk would be unnecessary — which
   makes the bug plausible and order-dependent (sensitive to dict iteration
   order).

**Action:** write a targeted test — `A(file_a) → B(file_b) → C(file_c)` with an
override of a `C` method on `A`, all three dirty in one batch — and assert the
`A.method OVERRIDES C.method` edge exists. If it reproduces, the fix (split into
an INHERITS pass for all dirty files, then an OVERRIDES pass) applies to **both**
build and incremental. If `class_supertypes` returns full ancestry, there is no
bug. **Do not act before this test.**

---

### 3.7 — LOW: symbol IDs are line-addressed for unqualified symbols

**[verified]** `symbol_id_from_symbol` (`identity.py:13-21`) uses
`qualified_name` when present, else falls back to `name@<line>`. Editing code
above an unqualified symbol shifts its line and changes its ID, causing
remove-then-re-add churn in `apply_delta` and "removed+added" instead of
"unchanged" in cross-revision diffs. The doc's "a symbol that didn't change
keeps its node id across revisions" is therefore only true for symbols with
qualified names.

**Impact:** low — most symbols (classes, methods, module-scope functions) carry
qualified names. The churn is confined to symbols ty does not qualify.

**Fix direction:** content-address unqualified symbols (hash the definition's
source text / AST subtree) instead of using the line number.

---

## 4. Structural / quality findings (Intern 3, verified where cited)

These are quality, not correctness. None threatens the MVCC isolation invariant.

- **[verified] `project.rs` is 2972 lines.** State types, ~20 `compute_*`
  cores, both builders, sync resolution, event synthesis, watcher drain-apply,
  all PyO3 method blocks, and three test modules in one file. Recommend splitting
  into `project/{state,build,sync,analysis,methods}.rs`.
- **[verified] `build_head` / `build_frozen` duplication.** The
  discover → clamp → `apply_configuration_files` → `fallible` →
  `use_defaults`-fallback chain is duplicated nearly verbatim
  (`project.rs:824-852` vs `875-903`); only `OverlaySystem::live` vs `frozen`
  and the return type differ. Extract a shared `build_database(root, system,
  label)`.
- **[verified] `eprintln!` in build paths** (`project.rs:848,898`). Fine for a
  CLI, wrong for an embedded library/server. Replace with `log::warn!`.
- **[reported, not re-verified] PySnapshot / PyHeadView read-method
  duplication.** Intern 3 reports ~20 read methods duplicated across the two
  view types differing only in how they acquire state; a macro or a `ReadSource`
  trait would collapse them. Consistent with the thin-wrapper pattern observed
  around `resolve_file_and_source`; worth confirming during the split.
- **[reported] Head-snapshot caching is check-then-act** (`_native` /
  `_invalidate_head_snap`): benign under the GIL but can leak an un-`close()`d
  snapshot on a lost race; old snapshots rely on GC rather than explicit
  `close()`. Low.
- **[reported] Misc:** `MAX_HEAD_RETRIES = 200` is high; `resolve_sync_path`
  (`project.rs:915`) permits `../` escape from root; `_infer_package` is a
  path-string heuristic that should be plumbed from `file_occurrences`. All Low.

---

## 5. Where the intern reviews landed vs. this assessment

| Finding | Intern 1 | Intern 2 | Intern 3 | This review |
|---|---|---|---|---|
| Disk content not revision-pinned (snapshot + sync + watcher + dir membership) | High (as 2 findings) | Medium (phase gap) | — | **HIGH, one root cause** — invariant the doc claims as done is not met |
| `edit_many` non-atomic | High | High | — | **HIGH — confirmed** |
| Virtual buffers write-only | High | Medium | — | **MEDIUM** — real API gap, capability exists |
| Stale docs/comments | Medium | Medium | — | **MEDIUM — confirmed** |
| HEAD-graph mutation lock | Medium | Low | — | **LOW now / latent HIGH** — contract must be decided |
| Inheritance OVERRIDES ordering | — | — | High (incremental) | **NEEDS TEST** — present in build too, not incremental-specific |
| Line-addressed symbol IDs | — | — | High | **LOW** — confirmed, narrow impact |
| `project.rs` size / build dup / eprintln | — | — | Critical/High | **Quality** — confirmed, do after correctness |

Net: Intern 1 was most right on the finding that matters most (the content
invariant); Intern 2 was right to narrow several severities to "scoped" but
under-rated the invariant gap by treating it as an unreached phase; Intern 3's
architecture read and structural findings are accurate, but its one correctness
claim (inheritance) is mis-scoped as incremental-specific and remains unverified.

---

## 6. Recommended sequencing

Correctness before structure.

1. **Fix `edit_many` atomicity** (§3.2). Small, unambiguous, has a clear repro.
2. **Make disk ingest store-backed** (§3.1). The real architectural fix: route
   lazy capture, `sync_path`, and the watcher through a revision-owned
   generation so the store is genuinely the system of record. Closes the
   headline gap and lets the doc's §2.2 invariant become true.
3. **Verify the inheritance ordering bug** (§3.6) with the A→B→C test. If real,
   split INHERITS/OVERRIDES passes in both build and `_index_files`.
4. **Decide the mutation concurrency contract** (§3.5): document single-writer,
   or add a session write lock spanning `edit*()` + `_apply_graph_delta()`.
5. **Correct stale comments/README** (§3.4).
6. **Structural refactor** (§4): split `project.rs`, extract `build_database`,
   adopt `log`, collapse view-method duplication.
7. Lower-priority: content-address symbol IDs (§3.7); explicit virtual read
   surface (§3.3); the misc Low items.

---

## 7. Verdict

The MVCC substrate is architecturally correct and its foundation
(per-revision independent storage) is the right, non-negotiable answer to
salsa's `cancel_others` constraint — and it is proven by the concurrency tests.
The implementation is mid-flight: solid for the agent-overlay workflow, with one
genuine invariant gap (disk content is not revision-pinned) that the
architecture doc currently overstates as complete, one clear atomicity bug, and
a well-understood set of quality/structure cleanups. None of the issues threaten
the core isolation guarantee for overlaid content, which is the library's
defining value. Close the content-invariant gap and the `edit_many` bug, decide
the mutation-concurrency contract, and the MVCC surface can honestly be called
done.
