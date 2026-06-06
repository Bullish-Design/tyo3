# Gates 1–3 — Refactoring & Remediation Guide

> **Purpose.** The Gates 1–3 implementation was tagged `gate3-complete`, but a
> review found the test suite is **not green** at that tag, the code layer's
> cross-file reference/import resolution is broadly broken, and several spec
> requirements from the original gate guides were not actually met. This guide
> walks you through fixing every issue, in dependency order, with a validation you
> must pass before moving on.
>
> **Read the three original guides first** (`GATE_1_CONTENT_STORE_GUIDE.md`,
> `GATE_2_IDENTITY_GUIDE.md`, `GATE_3_CODE_LAYER_GUIDE.md`). This document does not
> repeat their contracts; it assumes you know them and tells you what is wrong and
> how to make it right.

## How to work in this repo (unchanged)

- All commands run inside the dev shell. Prefix everything: `devenv shell -- <script>`.
- Rust tests: `devenv shell -- test-rust`. Full suite: `devenv shell -- tests`.
- Rebuild the native module after Rust changes: `devenv shell -- build`. **You must
  rebuild before Python tests will see Rust changes** — the `.so` is prebuilt.
- Never run `pytest`/`cargo` bare; always through `devenv shell --`.
- One labelled commit per validated step (`refactor: step N — …`). Never skip a
  validation. **The cardinal rule, broken last time: do not tag a gate complete
  until `devenv shell -- tests` is fully green.**

## The cardinal rule you must not break again

The previous pass tagged `gate3-complete` on a commit with at least six failing
committed tests. **A gate tag is a claim that the entire suite passes.** Before you
re-tag anything at the end of this guide, run `devenv shell -- tests` and paste the
final summary line (e.g. `N passed`) into the commit message. If anything fails or
is skipped to make it pass, the gate is not done.

---

## Part 0 — Establish ground truth (do this first)

**Goal.** Get an accurate, current census of what fails, so you can measure progress
and so "done" is verifiable.

**Do.**
1. `devenv shell -- tests 2>&1 | tee /tmp/baseline.txt`. Capture the full summary.
2. List every `FAILED` line. Known failures at the time of review (there may be more
   once these are fixed and downstream asserts start running):
   - `test_graph_incremental.py::test_apply_delta_revalidates_inbound_cross_file_edges`
   - `test_graph_incremental.py::test_importers_index_populated_after_build`
   - `test_graph_incremental.py::test_importers_index_survives_apply_delta`
   - `test_graph_update.py::TestRebuild::test_rebuild_preserves_incoming_references`
   - `test_graph_dependency.py::TestResolveExternal::test_resolves_from_cache`
   - `test_graph_semantics.py::test_reference_from_create_user_targets_user_class`
3. Commit the baseline file is **not** necessary — just keep it open. Do not change
   any test to make it pass except where this guide explicitly says a test was wrong.

**Acceptance gate:** you have a written list of current failures. Commit nothing yet.

---

## Step 1 — Fix the Rust `file_occurrences` provider (root cause)

**Goal.** Restore complete occurrence data. This is the **root cause** of almost all
the graph failures: project-local references never reach the Python layer, so no
reference edges, no import edges, and an empty reverse-dependency index are built.

**Evidence.** For the fixture:
```python
# models.py
class User:
    def save(self): ...
# app.py
from models import User
def run():
    return User().save()
```
`session.file_occurrences("app.py")` currently returns **only two** occurrences —
the `import` line and the `run` definition — and every field is `None`:
```
<ReferenceRole.IMPORT>      name=None tgt_file=None tgt_name=None tgt_qn=None
<ReferenceRole.DEFINITION>  name=None tgt_file=None tgt_name=None tgt_qn=None
```
The `User()` constructor reference and the `.save()` method reference are **absent
entirely**, and even the occurrences that are returned carry no `name` or resolved
target. The Python reference resolver (`graph.py:_resolve_references_via_occurrences`)
is correct in spirit but is being fed an empty stream, so it can only ever resolve
the stdlib cases that happen to come through a different path.

**Files.** `rust/src/convert/occurrences.rs`, `rust/src/dto/occurrences.rs`, and the
`file_occurrences` PyO3 method in `rust/src/project.rs` (grep for `file_occurrences`).

**Build.**
1. Find where `file_occurrences` is implemented and what ty/ruff API it calls
   (`ty_ide` occurrence/reference queries, e.g. the same machinery `document_highlights`
   and `goto_definition` use). Confirm whether the regression is:
   - **(a)** the provider only emitting `import`/`definition` roles and dropping
     `reference`/`read`/`call` roles, or
   - **(b)** the DTO mapping (`dto/occurrences.rs`) zeroing out `name`/`target_*`
     fields, or
   - **(c)** the provider never resolving targets (always returning `target_file =
     None`), forcing every consumer onto the goto fallback which then also fails.
2. Fix the provider so that for every name-like token in the file it emits an
   occurrence with: `role`, `name` (the source token text), `range`, and — when the
   token resolves to a definition — `target_file`, `target_name`,
   `target_qualified_name`. Project-local targets MUST resolve (this is the whole
   point). Model it on `goto_definition` if that path still works: it clearly can
   resolve `User` → `models.py::User`, so reuse that resolution per occurrence.
3. If `target_file` cannot be resolved for a given occurrence (genuine external/unknown),
   leave the target fields `None` — the Python layer already handles that by creating
   external stubs. But `name` must never be `None` for a real token.

**Validate.**
- Add a Rust unit test in `convert/occurrences.rs` over a two-file fixture asserting
  that `file_occurrences("app.py")` includes an occurrence whose `name == "User"`,
  `target_file` ends with `models.py`, and `target_name == "User"`; and an occurrence
  for `save` targeting `models.py`.
- `devenv shell -- build` then re-run the diagnostic above; confirm the `User`/`save`
  occurrences now appear with resolved targets.
- **Acceptance gate:** Rust test green; the diagnostic shows resolved project-local
  targets. Commit (`refactor: step 1 — restore complete file_occurrences data`).

> If `file_occurrences` is intentionally an "imports + definitions only" API and the
> richer per-token resolution was always supposed to come from a different call,
> then the bug is that the Python graph builder is calling the wrong API. In that
> case, fix `graph.py` to call the correct occurrence/reference query instead, and
> note the decision in a code comment. Either way, the success criterion is the same:
> project-local reference targets reach the Python layer.

---

## Step 1 — Critical context: session reads run over a *frozen snapshot* (the real root cause)

> **This is the single most important thing to understand before touching reads.**
> Fixing the `file_occurrences` provider (above) is necessary but **not sufficient**:
> when Step 1 was first done, the provider was correct (proven by a Rust unit test) yet
> cross-file imports *still* didn't resolve in any Python session, and the six baseline
> tests stayed red. The reason is below. It cost a long investigation; this section
> exists so the next person spends zero time on it.

### What actually happens on a read

`TyO3Session._native()` (in `src/tyo3/session.py`) does **not** return the live head
database. It returns a **cached frozen head snapshot**:

```python
def _native(self):              # TyO3Session override
    if self._head_snap is None:
        self._head_snap = self._inner.snapshot(None)   # a FROZEN snapshot at HEAD
    return self._head_snap
```

So `session.check()`, `session.file_occurrences()`, `session.goto_definition()`, and
**every `CodeGraph.build()`** run over the snapshot DB built by `build_frozen` /
`pre_populate_generation` (`rust/src/project.rs`) — over the **frozen** `OverlaySystem`
(`rust/src/overlay.rs`), *not* the live head. The only thing that reads the live head is
`session.latest` (`LatestView` → `PyHeadView`).

### Why that breaks cross-file resolution

The frozen `OverlaySystem` is **Design A**: it has **no disk fallback**. A frozen view
can only see what `pre_populate_generation` interned into its generation. ty's module
resolver, to resolve `from models import User`, must:

1. confirm each **search-root directory** exists (`path_metadata(<root>)` →
   `FileType::Directory`), and
2. find `models.py` / `models/` under it, then
3. read its content.

The original frozen `path_metadata` returned `not_found` for **anything that wasn't a
file document** — including **directories** (the generation stores only file keys, never
directory entries). So under a snapshot *every directory looked absent*, the resolver
found **no search roots**, and **no first-party module resolved at all** — not synthetic
fixtures, not the real `requests` repo, not even relative imports (`from .models import …`).
Stdlib still resolved because it comes from vendored typeshed, not the project tree — which
is exactly the misleading "stdlib resolves but project-local doesn't" symptom.

A second, related fidelity gap: `pre_populate_generation` only interned `.py`/`.pyi`
files, so `pyproject.toml` / `ty.toml` were unreadable through the frozen overlay. The
snapshot's `ProjectMetadata::discover` + `apply_configuration_files` therefore silently
ignored project configuration (e.g. `python-version`).

### The fixes (landed)

- **`overlay.rs` — synthesise directory metadata.** Frozen `path_metadata` now returns
  `FileType::Directory` for any path that is a strict ancestor of a generation key
  (`is_synthesised_directory`). This makes the frozen view fully self-describing for the
  module resolver **without** reintroducing a disk fallback (preserves §1.3.1).
- **`project.rs` — pre-populate config files.** `pre_populate_generation` now also
  interns `pyproject.toml`, `ty.toml`, `setup.cfg`, `setup.py` (`snapshot_relevant_file`),
  so snapshot discovery/config matches the head.
- **`occurrences.rs` — use the alias-following resolver.** Resolve targets with
  `ty_python_semantic::definitions_for_name` / `definitions_for_attribute` (with
  `ImportAliasResolution::ResolveAliases`), driven by a `SourceOrderVisitor` for complete
  coverage. **Do not** use `goto_definition` — it stops at the *local import binding*
  (`app.py`), not the original definition (`models.py`), which is what the first
  (failed) Step-1 attempt did.

### The invariant to keep (and the debugging trap)

> **Invariant.** A frozen snapshot's generation must be a *complete, self-describing*
> view: every project source file, every config file ty reads during discovery, and
> enough structure (directories synthesised from keys) that the module resolver can walk
> it with **no disk access**. If you add a new kind of file ty needs at analysis time,
> it must be added to `snapshot_relevant_file` too.

> **Debugging trap.** A Rust unit test that builds over `use_defaults` / `build_head`
> (live overlay, native disk fallthrough) can **pass** while the identical behaviour
> **fails** in a Python session — because the session goes through the frozen snapshot
> and the unit test does not. When a read-path bug reproduces in Python but not in Rust:
> 1. compare `session.check()` (frozen) against `session.latest.check()` (live head) —
>    if they differ, the bug is in snapshot fidelity, not in the provider;
> 2. look for `unresolved-import` diagnostics as the tell that the module resolver can't
>    see the tree;
> 3. only then suspect the occurrence/edge logic.

**Validate (snapshot fidelity).**
- A 2-file synthetic project (`from models import User; User().save()`) opened with
  `TyO3Session`: `session.check()` reports **no** `unresolved-import` for `models`, and
  matches `session.latest.check()`.
- The same for a real `src/`-layout project (e.g. `fixtures/demo_repos/requests`): no
  first-party `unresolved-import` diagnostics; the graph builds cross-file REFERENCES and
  IMPORTS edges.
- A `pyproject.toml` `python-version = "3.8"` is honored by `session.check()` (snapshot
  path), not just by the live head.

---

## Step 2 — Verify reference + import edges and the reverse-dep index rebuild

**Goal.** With Step 1's data flowing, confirm the Python reference resolver and
reverse-dependency index (Gate 3 §6.2.3 / Step 3) actually populate. Fix any residual
Python-side path-normalisation mismatch.

**Files.** `src/tyo3/graph/graph.py` —
`_resolve_references_via_occurrences` (≈ line 522), `_add_import_edge` (≈ 650),
`_normalize_result_path` (≈ 49), `_find_symbol_in_file` (≈ 497).

**Build / audit.**
- The resolver skips project-local targets it cannot map to a node:
  `graph.py:610-612` (`if target_file in project_files: continue  # identity mismatch`).
  Once Step 1 feeds real targets, confirm `target_file` (after `_normalize_result_path`)
  is the **same string key** the node was materialised under (project-relative, e.g.
  `models.py`). If the engine returns an absolute path and normalisation doesn't reduce
  it to the project-relative key, the lookup misses and the edge is silently dropped.
  Add a one-line assertion-style log (behind the existing `report` mechanism) when a
  project-local target fails to resolve, so this can never silently regress again.
- Confirm `_add_import_edge` records `self._file_importers[target_file].add(source_file)`
  (it does at `graph.py:688`) — this is the reverse-dep index. The bug was upstream
  (no edges were ever added), not here.

**Validate.**
- `devenv shell -- python -m pytest src/tyo3/tests/test_graph_incremental.py
  src/tyo3/tests/test_graph_semantics.py src/tyo3/tests/test_graph_dependency.py
  src/tyo3/tests/test_graph_update.py -q` — all green.
- Specifically the six baseline failures from Part 0 now pass.
- **Acceptance gate:** those four modules green. Commit
  (`refactor: step 2 — cross-file references & reverse-dep index restored`).

---

## Step 3 — Remove location-derived identity (Gate 2 §5.6 violation)

**Goal.** No node id, qualified path, or structural name may incorporate a line or
column number (§5.6, Gate 2 acceptance #7).

**Files.** `src/tyo3/graph/identity.py`.

**The offender.** `identity.py:46`:
```python
return f"{file}::{symbol.name}@{symbol.location.range.start.line}"
```
This fires whenever `session.id_for()` returns `None` (a freshly-created entity not
yet in the registry). It bakes a line number into the node key — exactly what the
spec forbids.

**Build.**
- The real fix is to **not have a None-identity path** during a build: the identity
  registry must be populated for the revision you are building over *before* you build.
  The commit transaction already reconciles before publishing (Gate 2 Step 6), so a
  graph built from a committed revision should always get a real `DurableId`. Audit the
  callers: `CodeGraph.build` is sometimes called before any `sync_all`/`edit` has
  populated the registry (see the `_graph_identity_primed` hack in
  `test_graph_incremental.py:43-50`). Make `build` defensively prime identity (or
  require a reconciled revision) rather than fall back to a line-keyed id.
- Replace the line-number fallback. Acceptable fallbacks, in order:
  1. Use the qualified-name key only: `f"{file}::{symbol.qualified_name or symbol.name}"`
     (no line). This is still location-free and stable for the cold path.
  2. Better: make the fallback impossible by ensuring identity is always primed (above),
     then turn the `None` case into a logged, reported `GraphBuildFailure` rather than a
     synthesised id.
- While here, fix the **stale docstring** in `derive_durable_id`: it claims "the
  session returns the enclosing class's DurableId (not a method-level id)", but
  `session.id_for` returns the **innermost** symbol's id (the method's own ULID — see
  `project.rs:1816-1837`). Either correct the comment or, if you intend methods to key
  off the class id, change the resolution deliberately — but do not leave the code and
  comment disagreeing.

**Validate.**
- `grep -rn "@.*line\|range.start.line}\|@{" src/tyo3/graph/identity.py` returns nothing.
- Add a test: build over a fixture where a brand-new file is created via `edit`, and
  assert every node key is a ULID or a `<module>`/`<external>` synthetic — never
  contains `@<digits>`.
- **Acceptance gate:** grep clean; test green. Commit (`refactor: step 3 — remove
  line-number identity fallback (§5.6)`).

---

## Step 4 — Populate node `content_hash` (Gate 3 §6.2.2)

**Goal.** Every entity node carries its content hash; §6.2.2 requires it and the
parity comparator (Step 8) will start checking it.

**Evidence.** Review showed nodes built with `content_hash=None`
(`SymbolNode(durable_id='01KT…', name='User', …, content_hash=None)`).

**Files.** `rust/src/dto/symbols.rs` (attach the hash to the symbol DTO — preferred),
`rust/src/project.rs` (the symbols/entities the session returns), and
`src/tyo3/graph/graph.py` `_materialize_file_nodes` (read it onto the node).

**Build.**
- The Gate 3 guide's preferred approach (Step 1 note) is to **attach the `DurableId`
  and `content_hash` to the symbol DTO the Rust layer already returns**, so the Python
  layer never re-derives them. Implement that: extend the symbol DTO with
  `durable_id: Option<String>` and `content_hash: Option<u128>` (or hex string),
  populated from the same `extract_entities`/registry data computed during the commit.
  This also removes the O(nodes) `session.id_for` FFI calls the Gate 3 guide warned
  about (and the line-keyed fallback from Step 3 disappears as a bonus).
- In `_materialize_file_nodes`, set `SymbolNode.content_hash` from the DTO field. If you
  defer the DTO change, at minimum populate `content_hash` via a session call so the
  field is non-`None` for every entity node.

**Validate.**
- Test: build over a fixture; assert every non-module, non-external node has a
  non-`None` `content_hash` that equals the registry's hash for that id.
- Test: a cosmetic (whitespace-only) edit leaves a node's `content_hash` unchanged
  across an incremental update; a body edit changes it. (Ties §6.2.2 to Gate 2 §5.5.)
- **Acceptance gate:** tests green. Commit (`refactor: step 4 — populate node
  content_hash (§6.2.2)`).

---

## Step 5 — Make reconciliation incremental / bounded (Gate 2 §5.5.6, acceptance #9)

**Goal.** A normal commit must reconcile only the touched files plus their reverse-dep
closure — not re-extract and re-hash the entire project on every keystroke.

**Evidence.** `commit_head` (`project.rs:1087-1090`) calls `extract_entities(&state)`,
which walks **`project.files()` — the whole project** (`entity.rs:86-101`), and
`reconcile`'s retire loop scans the **entire registry** (`identity.rs:733-739`). This
is O(project) per edit and violates §5.5.6. Full reconciliation is only acceptable on
`rescan`.

**Files.** `rust/src/entity.rs`, `rust/src/identity.rs`, `rust/src/project.rs`.

**Build.**
1. Add a scoped extractor: `extract_entities_for(state, files: &HashSet<String>) ->
   Vec<Entity>` that only walks the given files (refactor the existing whole-project
   `extract_entities` to delegate to it with the full file set, so the `rescan` path
   reuses it).
2. Compute the dirty set in the commit transaction from the `Change`s / `ChangeEvent`s
   already in hand (the files written this commit), unioned with their reverse-dependency
   closure. The reverse-dep information lives in the code-layer graph
   (`importers_of`) — the commit path will need access to it, or you compute the
   closure from ty's module dependencies. Document the closure source.
3. Add a **scoped reconcile**: `reconcile_scoped(registry, entities, revision, scope:
   &HashSet<String>)`. The retire loop must only consider anchors **whose
   `qualified_path` file is in `scope`** — an anchor outside the scope was not
   re-extracted and MUST NOT be retired. This is the critical correctness fix: the
   current unscoped retire loop would delete the entire rest of the project the moment
   you feed it a dirty subset. (It is only "safe" today because it is always fed the
   whole project — which is the perf bug.)
4. Keep the full-project path for `rescan`/`sync_all` only.

**Validate.**
- Instrumentation test (mirror Gate 2 acceptance #9): edit one file in a multi-file
  project; assert the number of entities extracted/reconciled is bounded by the
  touched+closure set, not the project size. (Add a debug counter or return the count.)
- Regression: editing one file does **not** retire or churn ids of unrelated files
  (assert their `id_for` is unchanged and they do not appear in `SyncResult.orphaned`).
- All existing identity tests (`test_gate2_identity.py`) still green.
- **Acceptance gate:** bounded-cost test green; no cross-file id churn. Commit
  (`refactor: step 5 — bounded incremental reconciliation (§5.5.6)`).

> This is the highest-risk change in the guide. The scoped retire is subtle: get the
> "only retire anchors inside the reconciled scope" rule exactly right, and add a test
> that a symbol genuinely deleted **within** a touched file is still retired/orphaned
> (so you don't over-correct and stop retiring real deletions).

---

## Step 6 — Fix `orphaned` re-firing

**Goal.** An anchor orphaned in revision R must not be re-reported as newly orphaned on
every later unrelated commit.

**Evidence.** `reconcile`'s retire loop (`identity.rs:732-739`) pushes **every** anchor
not in `bound_ids` into `recon.retired` each call. An anchor orphaned 50 commits ago is
never in `bound_ids` (its entity is gone), so it is `retire()`d again and re-emitted in
`SyncResult.orphaned` on every commit. (Step 5's scoping reduces but does not eliminate
this.)

**Files.** `rust/src/identity.rs`.

**Build.**
- Only emit an id in `recon.retired` on the commit where it **transitions** to
  `Orphaned` — i.e. when its status was not already `Orphaned`. `retire()` should be a
  no-op (status-wise) and must not push to `retired` if the anchor is already orphaned.
- `session.orphaned()` (the steady-state query) already filters by status and is fine;
  this fix is about the per-commit `SyncResult.orphaned` delta being a true delta.

**Validate.**
- Test: delete a symbol at R → it appears in `SyncResult.orphaned` exactly once (at R);
  a subsequent unrelated edit at R+1 does **not** list it again.
- Test: re-add the symbol (Gate 2 §5.5 rule 5) → it re-binds to the retained id and
  status returns to `Active`.
- **Acceptance gate:** green. Commit (`refactor: step 6 — orphaned delta fires once`).

---

## Step 7 — Rewrite the §6.4 inheritance test to actually reproduce the hazard

**Goal.** The two-pass inheritance guarantee (§6.4) must be exercised by the
**cross-file, multi-level, processing-order-parameterised** case the original guide
mandated — not the single-file stand-in currently present.

**Evidence.** `src/tyo3/graph/tests/test_inheritance_ordering.py` uses a **single file**
holding all three classes, with a comment that the engine only resolves supertypes
within a file. A single file cannot reproduce the hazard (which is about an intermediate
ancestor's `INHERITS` edge not existing yet when a different file's `OVERRIDES` is
computed), and the test does not parameterise processing order at all. The Step-0 commit
also recorded `status=LIVE` while the test asserts the edge *exists* (i.e. passes) —
contradictory.

**Files.** `src/tyo3/graph/tests/test_inheritance_ordering.py` (rewrite).

**Build — the fixture the original guide specified (three files):**
```python
# c.py
class C:
    def greet(self): ...
# b.py
from c import C
class B(C): ...
# a.py
from b import B
class A(B):
    def greet(self): ...   # OVERRIDES C.greet, two levels up
```
- Full build: assert `A.greet --OVERRIDES--> C.greet` exists.
- Incremental: build, then `edit_many` all three files in one batch, `apply_delta`,
  assert the edge still exists.
- **Parameterise the dirty-file processing order** (`a,b,c` and `c,b,a`) — the test must
  pass in **both**. If your passes iterate a dict, force the order by sorting the file
  set with a parametrised key so the test genuinely drives both orders.
- If the engine truly cannot resolve cross-file supertypes (so `class_supertypes` returns
  nothing across files), that is itself a bug to fix or escalate — the code layer cannot
  build inheritance across modules without it. Investigate `session.class_supertypes`
  for the cross-file case before settling for the single-file fixture; document the
  finding in the test module.

**Validate.**
- `devenv shell -- tests -k inheritance_ordering` green in both orders, full + incremental.
- Record the real status (LIVE/LATENT) honestly in the commit message.
- **Acceptance gate:** green in all orders. Commit (`refactor: step 7 — §6.4 test
  reproduces the cross-file hazard`).

---

## Step 8 — Strengthen the parity comparator and complete the parity suite (§6.3.1)

**Goal.** Parity must compare enough that "incremental == rebuild" actually means the
graphs are equivalent — not just that they share node ids and edge kinds. The weak
comparator is why `content_hash=None` and the broken reference edges sailed through.

**Files.** Create `src/tyo3/graph/tests/test_incremental_parity.py` (the Gate 3 Step 7
file that was never written); you may lift helpers from `test_graph_incremental.py`.

**Build the comparator** (per Gate 3 Step 7):
- Same set of node `DurableId`s.
- **Per node**, the same `(kind, qualified_name, file, content_hash)`. (This is the part
  that was missing — add it. Normalise location/range only if both sides agree.)
- Same set of edges as `(src_id, dst_id, edge_kind, role)` tuples — compare as **sets**,
  sorted with a total key, never `repr`. (Add `role`/extra; today only
  `(src,dst,kind)` is compared.)

**Build the scenarios** (each: run one graph via `build`, one via a sequence of
`apply_delta`, to the same final revision; assert equal):
- single-file content change; file creation; file deletion;
- cross-file reference added, then removed;
- cross-file inheritance added; **the multi-level §6.4 chain edit**;
- a **`moved` entity** (rename/move with unchanged body) — assert node id preserved,
  location updated, no edge churn;
- a `rescan` delta equals a fresh build;
- a **randomised sequence** (fixed seed): N random edits, compare to a rebuild at the
  end. Wire this into `devenv shell -- test-property` if expressed via Hypothesis.

**Validate.**
- Every scenario equal under the **strengthened** comparator.
- Do **not** weaken the comparator to make a test pass — a divergence is a real bug
  (usually a stale reverse-dep entry or an inbound-revalidation miss). Fix the update.
- **Acceptance gate:** all parity scenarios green. Commit (`refactor: step 8 —
  strengthened parity comparator + full scenario suite (§6.3.1)`).

---

## Step 9 — Decide and document frozen `walk_directory` (Gate 1 Steps 6 & 10)

**Goal.** Close the frozen-view isolation hole: a pinned snapshot must not enumerate
disk files outside its generation.

**Evidence.** `overlay.rs:303-314`: `walk_directory` delegates to `native` even when
`frozen.is_some()`, with a comment acknowledging it. Step 10's audit checklist requires
"no native content/dir read reachable in a frozen view." `read_directory` was done
correctly from the generation; `walk_directory` was not. ty's project discovery uses
`walk_directory`, so a frozen db can discover disk files not pinned at its revision.

**Files.** `rust/src/overlay.rs`.

**Build — pick one and document the decision in the module comment:**
- **Preferred:** implement frozen `walk_directory` to enumerate from the generation
  (descendants of `path` that are `Document::Text`), mirroring the frozen
  `read_directory` logic. The blocker noted in the comment is that
  `ruff_db::walk_directory::DirectoryEntry` isn't publicly constructible — resolve by
  (a) upstreaming/patching a constructor, or (b) building the walk result from
  `read_directory` recursively over generation keys (you already enumerate children;
  recurse into synthesised directories).
- **If genuinely infeasible now:** this is the explicit §6 escalation trigger, not a
  silent delegation. Escalate, and as an interim guard make frozen `walk_directory`
  return only generation-backed entries even if that means a thinner walk — never live
  disk under a frozen view.

**Validate.**
- Test (mirror Gate 1 Step 6 gate): build a frozen view; create a new file on disk
  afterward; assert it is **absent** from the frozen `walk_directory`, present in a live
  one.
- Re-run the Step 10 audit grep: no `self.native.*` content/dir read reachable when
  `frozen.is_some()`.
- **Acceptance gate:** test green; audit clean or escalation recorded. Commit.

---

## Step 10 — Add the missing Gate 1 Python acceptance module

**Goal.** Gate 1's seven final-acceptance invariants must have Python-level tests, as
the guide specified ("a dedicated test module proves each"). Today they exist only as
Rust unit tests, and `RevisionEvictedError` has **zero** Python coverage.

**Files.** `src/tyo3/tests/test_gate1_content_store.py` (new).

**Build — one test per Gate 1 §"Final acceptance" item, through the Python API:**
1. Same-revision determinism (§1.3.1): two `snapshot(at=R)` with disk mutated between
   them → identical content and directory listings.
2. Membership pinning (§1.3.3): a file created on disk after a snapshot is absent from
   that snapshot's enumeration, present in a live view.
3. Ingest records content (§1.3.2): `snapshot(at=synced_R)` stable after a later disk
   change.
4. Batch atomicity (§6.1.1): `edit_many` advances the revision exactly once; no
   half-batch revision observable.
5. **Eviction error (§1.3.6): `snapshot(at=evicted_R)` raises `RevisionEvictedError`**
   (importable from `tyo3`), not a panic. (Currently untested in Python.)
6. Isolation/liveness (§2): existing concurrency stress still shows writer never blocks,
   reads never cancelled (reference `test_mvcc_concurrency.py`).
7. O(1) publish (§1.3.4): publishing over a large project does no per-file work
   (timing/instrumentation).

**Validate.** Module green. **Acceptance gate:** committed
(`refactor: step 10 — Gate 1 Python acceptance module`).

---

## Step 11 — Tag hygiene

**Goal.** Tags must mean what they say.

**Do.**
- There is **no `gate1-complete` tag**, yet Gates 2–3 were built on it. After Steps 1–10
  pass and `devenv shell -- tests` is fully green, document that Gate 1 acceptance is proven by
  Step 10's module on the current commit).
- Re-validate `gate2-complete` and `gate3-complete`: ensure both point at commits where
  the **full suite** passes. 
- In each re-tag's annotation, paste the `devenv shell -- tests` summary line.

**Acceptance gate:** every gate tag points at a green commit; annotations include the
passing summary.

---

## Final acceptance — all must hold before re-tagging `gate3-complete`

Run `devenv shell -- tests` **and** `devenv shell -- test-property`. Confirm:

1. **Suite fully green.** Zero failures, zero tests skipped-to-pass. Paste the summary.
2. **Cross-file references & imports resolve** (Step 1–2): the six baseline failures pass;
   `file_occurrences` returns resolved project-local targets.
3. **No location-derived identity** (Step 3): grep is clean.
4. **Node `content_hash` populated** (Step 4): no `content_hash=None` on entity nodes.
5. **Bounded reconciliation** (Step 5): one-file edit reconciles only touched+closure;
   no unrelated-file id churn; real in-file deletions still retire.
6. **`orphaned` is a true delta** (Step 6): fires once per actual orphaning.
7. **§6.4 reproduced** (Step 7): three-file chain, both processing orders, full + incremental.
8. **Strengthened parity** (Step 8): payload + edge-role comparison; moved-entity and
   randomised scenarios pass.
9. **Frozen isolation closed** (Step 9): frozen `walk_directory` never reads live disk,
   or escalation recorded.
10. **Gate 1 acceptance proven in Python** (Step 10), including `RevisionEvictedError`.
11. **Tags honest** (Step 11).

When all hold on a clean `devenv shell -- tests`, re-tag and record the passing summary
in the tag annotation.

---

## Appendix — quick file/line index of the issues this guide fixes

| Issue | Location | Step |
|---|---|---|
| `file_occurrences` returns empty/None occurrences; uses `goto_definition` (stops at import binding) instead of alias-following `definitions_for_name`/`_for_attribute` | `rust/src/convert/occurrences.rs` | 1 |
| **Reads run over a frozen snapshot; frozen `path_metadata` returns `not_found` for directories → module resolver blind → NO first-party import resolves** | `rust/src/overlay.rs` (`path_metadata`, `is_synthesised_directory`) | 1 (context) |
| Snapshot pre-population omits config files → `pyproject.toml`/`ty.toml` unreadable in snapshot → project config (e.g. `python-version`) ignored | `rust/src/project.rs` (`pre_populate_generation`, `snapshot_relevant_file`) | 1 (context) |
| Project-local refs skipped; reverse-dep index empty; `Import`-role occurrences create spurious REFERENCES edges from module node | `graph.py` (`_resolve_references_via_occurrences`), `_add_import_edge` | 2 |
| Line-number-derived id fallback (§5.6) | `graph/identity.py:46` | 3 |
| Stale `derive_durable_id` docstring vs `id_for` | `graph/identity.py:19-54`, `project.rs:1816-1837` | 3 |
| Node `content_hash` is `None` (§6.2.2) | `dto/symbols.rs`, `graph.py:_materialize_file_nodes` (287) | 4 |
| Whole-project extraction/reconcile every commit (§5.5.6) | `entity.rs:86`, `identity.rs:733`, `project.rs:1087` | 5 |
| `orphaned` re-fires every commit | `identity.rs:732-739`, `retire()` (183) | 6 |
| §6.4 test is single-file, no order param | `graph/tests/test_inheritance_ordering.py` | 7 |
| Parity comparator too weak; missing scenarios | `test_graph_incremental.py:18-40`; new `test_incremental_parity.py` | 8 |
| Frozen `walk_directory` delegates to disk | `overlay.rs:303-314` | 9 |
| No Gate 1 Python acceptance module; no `RevisionEvictedError` test | new `test_gate1_content_store.py` | 10 |
| No `gate1-complete` tag; `gate3-complete` on red commit | git tags | 11 |
