# Phase 4 — Cutover: the Python graph becomes a pure applier

> A step-by-step execution guide for **Phase 4** of
> `REFINED_IMPLEMENTATION_PLAN.md`. Read that plan's "Phase 4" section and §5.3 /
> §5.4 / §5.9 of `REFINED_IMPLEMENTATION_CONCEPT.md` once before starting — this
> guide assumes that vocabulary (the code delta, the pure applier, read-side
> writes, the parity oracle, revision-gating) and turns it into concrete edits
> against the code as it exists today.
>
> **Phase 4 depends on Phases 1, 2, and 3.** Phase 1 made committed generations
> complete and reconciled identity *at open*. Phase 2 added the native `CodeLayer`
> and `CodeDeltaDto`, produced inside the commit, **proven equal to the legacy
> Python build via the parity oracle's strict structural tier** (node set,
> structural node fields, edge relation set; cosmetic diffs surfaced and known),
> and a *minimal* pure
> `CodeGraph.apply_code_delta` pulled forward so the oracle could run. Phase 3
> reshaped the public write result into the id-level `CommitDelta` with the Phase 2
> `code_delta` **nested** inside it. Phase 4 is the **cutover**: the native code
> delta becomes *authoritative*, the Python graph stops building from the read
> surface, and every read-side write is deleted. This is where the bulk of the old
> graph-construction code goes away. If any earlier phase's milestone gate is not
> green — **especially Phase 2 parity** — stop and finish it first. You are about
> to delete the very code parity proves the native side reproduces; do not delete
> it until parity is green.

---

## 0. The dev environment — read this first, it governs every command below

This repository is a **devenv.sh (Nix-managed) dev environment**. The toolchain
(the right `rustc`, `cargo`, `maturin`, `python`, `pytest`, `ruff`, and the
project's custom scripts) only exists *inside* the devenv shell. A `cargo` or
`pytest` run from a bare login shell is either missing or the wrong version and
will give you misleading results.

**The rule for the entire phase: every in-repo operation runs through the devenv
shell.** Two equivalent forms:

- One-shot (preferred in this guide, copy-pasteable):
  ```bash
  devenv shell -- pytest src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov
  ```
- Interactive (if you are iterating rapidly):
  ```bash
  devenv shell        # drop into the environment once
  # …then run cargo / pytest / the project scripts directly inside it…
  ```

The project defines custom scripts inside `devenv.nix` — `build`, `tests`,
`test-rust`, `test-quick`, `clean`, `status`, etc. When this guide says "rebuild
the extension," it means `devenv shell -- build` (which runs maturin so Python
sees the freshly compiled Rust). **A pure `cargo test` does not refresh the
compiled extension Python imports.** Phase 4 flips the *authoritative* graph path
to the native delta and (4.4) needs a native full-code-delta accessor on the
snapshot handle — both observed from Python — so after **every** Rust change the
Python suite must observe, run `devenv shell -- build` before the pytest gate.
Forgetting this is the single most common way to chase a phantom failure (the
parity or no-read-side-writes test sees the *old* compiled behaviour).

The suites are slow. Allow **~10–15 minutes** for a full run; launch full suites
in the background and give them time rather than assuming a hang.

> Throughout the rest of this document, **assume every `cargo`, `pytest`,
> `maturin`, `ruff`, and project-script invocation is prefixed with
> `devenv shell --`**, even where a line is abbreviated for readability.

---

## 1. What Phase 4 changes, and why

**Goal (from the plan):** the native code delta becomes **authoritative**; the
Python graph stops building from the read surface and stops triggering writes
(§5.3, §5.9). This is where the bulk of the old graph-construction code is
deleted.

**The situation today** (confirmed in the current code):

- **The graph is built and updated entirely in Python from the read surface.**
  - `CodeGraph.build` (`src/tyo3/graph/graph.py:158`) is the six-pass
    read-surface construction (collect symbols → materialise nodes → containment
    + range caches → reference resolution → two-pass inheritance → diagnostics,
    `graph.py:199-253`). Every pass calls into the native analysis read surface
    over FFI (`document_symbols`, `file_occurrences`, `class_supertypes`, …).
  - The **incremental** updater `apply_delta` (`graph.py:1924`) is the
    path-shaped post-write update: it drops nodes of dirty files, **re-extracts
    them from the read surface** (`_index_files`, `graph.py:1319`), and
    revalidates inbound edges (`_resolve_references_via_occurrences`,
    `_revalidate_inbound_inheritance`). It consumes the path-shaped delta
    (`delta.created/changed/deleted/moved` as file/qualified-path strings).
- **A read performs a write.** `CodeGraph.build` calls
  `_prime_identity_registry(session)` (`graph.py:184` → `graph.py:34`), which
  invokes **`session.sync_all()`** — a write that advances the revision — guarded
  by the `_graph_identity_primed` flag (`graph.py:36`,`:43`). So reading
  `session.graph` (`session.py:733`) can change `session.head`. This is §6.3
  defect #3 and the exact thing `test_final_no_read_side_writes.py` forbids.
- **The post-commit path drives the legacy updater.** Every write method funnels
  through `_apply_graph_delta` (`session.py:1103`), which calls
  `self._head_graph.apply_delta(self, result)` — the read-surface incremental
  updater — then `_invalidate_derived` (`session.py:753`).
- **The snapshot graph rebuilds from the read surface too.** `Snapshot.graph()`
  (`session.py:1484`) calls `CodeGraph.build(self, root=self._root)._pin_at(...)`
  — a full read-surface walk over the snapshot, *and* (via `build`) the same
  `_prime_identity_registry` write-side path on the owning session.
- **The floating latest view delegates to the session graph.**
  `LatestView.graph()` (`session.py:1694`) returns `self._session.graph`, so it
  inherits whatever the session graph does (including, today, the prime).

**What Phases 2–3 already put in place (you build on it, don't rebuild it):**

- A native `CodeLayer` + `produce_code_delta(...)` (Phase 2), producing a delta
  inside the commit, **stored back into `head.code_layer`**, proven
  structurally parity-equal to the legacy build (cosmetic tier surfaced/known).
- `PyTyProject.full_code_delta()` (Phase 2) — a *full* (`rescan=true`, scope=all)
  pythonized code delta for the current head state. The parity oracle probes it
  (`parity_oracle.py:233-247`).
- A *minimal* pure `CodeGraph.apply_code_delta(code_delta)` (Phase 2 pull-forward)
  used only by the oracle so far — Phase 4 promotes and hardens it.
- The id-level `CommitDelta` (Phase 3) with the Phase 2 code delta **nested** as
  its `code_delta` field (Rust `CommitDeltaDto.code_delta`; Python
  `CommitDelta.code_delta`, currently a `dict | None`).

**The fix this phase delivers:**

1. **A real, hardened pure applier** `CodeGraph.apply_code_delta` (4.1): upserts
   nodes, adds/removes edges, maintains the secondary indices (including the
   file-level reverse-dependency index) **from the DTO only** — no FFI, no
   read-surface reads, touching no session or snapshot.
2. **The native delta becomes authoritative** (4.2): the post-commit graph update
   calls `apply_code_delta(delta.code_delta)` (revision-gated), and the entire
   read-surface construction in `graph/graph.py` is deleted.
3. **No read-side writes** (4.3): `_prime_identity_registry` and its flag are
   gone; reading the graph never calls `sync_all`. Identity was reconciled at
   open (Phase 1.3) and on every commit (Phase 3), so it is always populated
   before a read. A snapshot graph build that finds identity missing raises a
   typed build failure — it never mutates the session.
4. **The snapshot graph is built from native state** (4.4): a snapshot's
   `graph()` applies a native code delta computed over the **snapshot's own frozen
   database**, consistent with its pinned revision, mutating no session state.

### Why this is the cutover, and why parity is the seatbelt

Phase 2 proved — for *every parity fixture* — that the native-delta graph equals
the legacy read-surface graph in the **structural tier** (node set, structural
node fields, edge relation set), with any cosmetic divergence surfaced and
understood. Phase 4 acts on that proof: it makes the native delta the *only*
source of the graph and deletes the legacy build. The risk is entirely "did I
delete something the native side does not yet reproduce?" — which is precisely
what the parity oracle answers mechanically. **Keep the parity suite green at every
step of this phase**, including after each deletion. A *structural* parity failure
means you deleted ahead of the native side; revert that deletion and fix the
producer (Phase 2 territory), not the test. (A cosmetic warning that newly appears
is also a signal — investigate it — but it is not, by itself, a stop-the-line
failure the way a structural diff is.)

### Files in scope

| File | Role in Phase 4 |
|---|---|
| `src/tyo3/graph/graph.py` | **edit (large delete)** — harden `apply_code_delta` (4.1); delete `build`'s read-surface passes, `apply_delta`, the resolution/inheritance/materialisation helpers, `_prime_identity_registry` (4.2, 4.3) |
| `src/tyo3/session.py` | **edit** — `graph` property + `_apply_graph_delta` use the native delta, revision-gated; `Snapshot.graph()` builds from the snapshot's native delta; drop the `_graph_identity_primed` flag usage (4.2, 4.3, 4.4) |
| `rust/src/project.rs` | **edit** — expose a `full_code_delta()` on the **snapshot/frozen** native handle (mirror of the head accessor) computed over the frozen database (4.4) |
| `src/tyo3/graph/models.py` | reference — `SymbolNode` / `EdgeData` payload contract the applier fills; `GraphBuildFailure` is the typed read-build failure (do not change shapes) |
| `src/tyo3/tests/parity_oracle.py` | reference — the comparator + `native_delta_graph` seam; do **not** weaken it |
| `src/tyo3/tests/test_final_no_read_side_writes.py` | the Phase 0 test that must go green this phase (do **not** weaken it) |
| `src/tyo3/tests/test_graph*.py` | the regression surface that must stay green over the native applier |

---

## 2. Working rules for this phase (non-negotiable)

1. **Structural parity stays green at every step.** Before each deletion in 4.2,
   confirm parity's structural tier is green; after each deletion, re-run it. The
   legacy build is your reference *until you delete it* — so delete it **last**,
   only once the native path drives both the head graph and the snapshot graph and
   structural parity still holds. (Cosmetic warnings are watched, not gating.)
2. **The applier is pure.** `apply_code_delta` makes **no FFI calls, no
   read-surface reads, and touches no session or snapshot** — it is a pure
   function of `(self, code_delta)` (§5.3 / Phase 4.1). The temptation to "read
   one more thing off the session" to patch a gap is the exact coupling this phase
   removes; fix the *producer* (Phase 2) instead.
3. **Reads never write.** After this phase, no read accessor —
   `session.graph`, `Snapshot.graph()`, `LatestView.graph()`,
   `LatestView.check()` — may advance `session.head`. Prove it with the seam
   `test_final_no_read_side_writes.py`, not by inspection.
4. **Revision-gate the apply.** A code delta is applied only if it advances the
   graph from its current revision by exactly the expected step; an out-of-order
   or stale delta must be impossible to apply (it triggers a rebuild-from-full or
   is rejected, never a silent partial). This backs the §5.3 "publication is the
   last in-lock step, readers observe fully-updated layers" guarantee.
5. **Don't swallow errors.** A snapshot graph build that finds required identity
   missing raises a **typed** `GraphBuildFailure` (`graph/models.py`), never a
   silent empty graph and never a `sync_all` to "repair" it.
6. **Never weaken a test or the comparator's structural tier.** If the
   no-read-side-writes test or a `test_graph*` case fails, the applier/producer is
   wrong, not the test. Do not narrow the structural tier or reclassify a field to
   make a deletion "pass."
7. **Milestone gate after the phase** (both must pass, both via devenv):
   ```bash
   devenv shell -- pytest -q --no-cov
   devenv shell -- cargo test --manifest-path rust/Cargo.toml
   ```

---

## 3. Pre-flight — establish the red baseline

Confirm the starting state before changing anything, so you can prove your work
moved the needle.

```bash
# 1. Phases 1–3 are landed and the Rust core is green (incl. the code layer/delta).
devenv shell -- cargo test --manifest-path rust/Cargo.toml content overlay project entity identity code_layer code_delta commit_delta

# 2. Parity is GREEN — the native delta reproduces the legacy build (your seatbelt).
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py -q --no-cov -rA

# 3. The no-read-side-writes contract is RED in exactly the expected way:
#    all three cases xfail(strict) because session.graph / snapshot.graph() prime
#    identity via sync_all() (a read-side write).
devenv shell -- pytest src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov -rA

# 4. The legacy graph behaviour suite is green — your regression target.
devenv shell -- pytest src/tyo3/tests/test_graph*.py -q --no-cov
```

Record the output. Two North Stars: (a) **parity is green** — if it is not, you
cannot safely delete the legacy build; finish Phase 2 first. (b) the three
strict-xfails in `test_final_no_read_side_writes.py` exist precisely because the
graph read still calls `sync_all`. By the end of Phase 4 those flip to real green
assertions and the markers are removed, and parity is *still* green over the
native-only path.

---

## 4. Step-by-step implementation

Do the steps in order. Each step lists **what**, **why**, **where**, and a
**verify** command (devenv-prefixed). Commit at the natural breakpoints noted.

> **Sequencing matters and is deliberate.** Harden the applier (4.1) and make it
> *authoritative* (4.2 first half) **before** deleting anything. Then delete the
> read-surface build (4.2 second half) with parity still guarding you. Then remove
> the read-side write (4.3) and re-home the snapshot graph (4.4). Deleting before
> the native path drives the graph would leave you with no working graph and no
> parity reference.

### Step 4.1 — Harden the pure applier on `CodeGraph`

**What.** Turn the Phase 2 *minimal* `apply_code_delta` into the real one:
`apply_code_delta(self, code_delta) -> None` that, from the `CodeDeltaDto` (a
pythonized dict or typed mirror) alone:
- **upserts** nodes from `nodes_upserted` (construct/replace `SymbolNode`s),
- **removes** nodes in `nodes_removed` (durable ids),
- **applies** `nodes_moved` as in-place location updates (new file/range, **same
  DurableId, no edge churn** — §5.5),
- **adds** `edges_added` as `EdgeData`, **removes** `edges_removed`,
- **updates every secondary index** the query methods rely on
  (`_id_to_index`, `_file_to_nodes`, `_file_to_edges`, and the file-level
  reverse-dependency index `_file_importers`) **from the delta**, and
- handles a **full/`rescan` delta** (everything in `nodes_upserted` + all
  `edges_added`, `rescan=true`) by clearing the graph first and applying wholesale.

**Why.** §5.3 / Phase 4.1. This is the single mechanism by which the graph is
ever updated after Phase 4 — both the post-commit head update (4.2) and the
snapshot build (4.4) go through it. It **must** reproduce the payloads the parity
comparator's structural tier checks — every structural `SymbolNode` field
(`durable_id, name, qualified_name, kind, file, range, content_hash, external`)
and the edge **relation** (`source, target, kind`) — and should also fill the
cosmetic fields (`selection_range, content_hashes, package`; edge `file, range,
role`) so the cosmetic tier stays quiet. Build the `SymbolNode`/`EdgeData`
faithfully; the structural tier is what gates, the cosmetic tier is what you keep
clean.

**Where / how.**
- Start from the Phase 2 applier and the *shapes* the legacy build produced — the
  Phase 2 guide's "parity field contract" table is the mapping you must honour
  (`name_range → selection_range`, lowercased `kind → SymbolKind`, hex
  `content_hash`, `external`/`package` for synthetics, `content_hashes` matching
  whatever the legacy build emitted — today `{}`).
- **Reuse, don't reinvent, the low-level mutators.** The existing
  `_add_node` (`graph.py:1141`) and `_add_edge` (`graph.py:1166`) already maintain
  `_id_to_index` / `_file_to_nodes` / `_file_to_edges` correctly. Build the applier
  on top of them so index maintenance has one authority. (`_add_edge` resolves
  endpoints via `_id_to_index`, so **apply all `nodes_upserted` before any
  `edges_added`** — the same node-before-edge ordering the producer guarantees;
  an edge whose endpoint has not been inserted yet must be a producer bug, not a
  silently dropped edge.)
- **Reverse-dep index from the delta.** The file-level `_file_importers`
  (`graph.py:118`) was historically maintained inside `_add_import_edge`
  (`graph.py:725`). Drive it from the delta now: for each `imports`/`references`
  edge between two project files, record `target_file → source_file`. Removals
  (`edges_removed`) must symmetrically prune it. Keep this incremental — Phase 6's
  bus file-interest matching and the dependency queries (`_importers_of`,
  `dependencies`/`dependents`) read it.
- **Node removal must keep indices consistent.** `rustworkx` `remove_nodes_from`
  invalidates stored integer indices (swap-and-pop). The legacy
  `apply_delta` handled this by calling `_rebuild_indexes` (`graph.py:1184`) after
  bulk removal. Either reuse `_rebuild_indexes` after removals, or remove
  carefully and patch the indices — but **do not** leave a dangling `_id_to_index`
  entry pointing at a reused slot. `_rebuild_indexes` is the safe default; keep it.
- **Revision bookkeeping.** Set `self._revision` from the delta's `revision`
  after a successful apply (the gating logic in 4.2 reads it).
- **Purity guard.** No `source`/`session` parameter; no `*.files()`, no
  `_collect_symbols_for_file`, no `document_symbols`/`file_occurrences` calls. If
  you find yourself needing one, the producer is under-supplying a field — fix
  the DTO/producer (Phase 2), not the applier.
- **Drop the dead resolution-time indices.** `_name_to_id` (`graph.py:133`),
  `_name_prefix_index` (`graph.py:138`), and `_file_node_ranges` /
  `_build_range_cache_for_file` (`graph.py:128`,`:403`) existed only to *resolve*
  symbols and enclosing ranges during the read-surface passes. Resolution now
  happens in Rust, so these become dead in 4.2 — do not feed them from the
  applier. (You delete their definitions in 4.2; the applier simply never touches
  them.)

**Verify.**
```bash
devenv shell -- build
# The applier alone, against a cold full delta, must reproduce the legacy graph:
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py -q --no-cov -rA
```
Add focused applier unit tests (a new `test_graph_apply_code_delta.py` or extend
`test_graph_update.py`): applying a full delta then an incremental
`nodes_upserted` of one changed node leaves the rest of the graph byte-identical;
a `nodes_moved` entry changes only the node's file/range and leaves its edges
intact; an `edges_removed` prunes `_file_importers`; applying with `rescan=true`
replaces the whole graph.

> Commit here: `feat(graph): hardened pure apply_code_delta (nodes/edges/moves/indices, no FFI)`.

---

### Step 4.2 — Make the native delta authoritative, then delete the legacy build

This is two moves in one step, in this order: **(a)** flip the authoritative path
to the native delta while the legacy build still exists (so you can A/B with
parity), then **(b)** delete the legacy read-surface code once the native path
drives everything and parity is green.

**Why.** §5.3 / §5.9 / Phase 4.2. The graph must be a pure projection of published
revisions applied from the native delta; the read-surface build is the split-brain
this whole refactor removes.

#### (a) Flip the post-commit head update to the native delta (revision-gated)

**Where / how.**
- Rewrite `_apply_graph_delta` (`session.py:1103`) to apply the **nested** code
  delta instead of calling the legacy `apply_delta`:
  ```python
  def _apply_graph_delta(self, delta: CommitDelta) -> None:
      if self._head_graph is None:
          return                      # graph not materialised → nothing to update
      code_delta = delta.code_delta   # Phase 3 nested the Phase 2 code delta here
      if code_delta is None:
          # No structural delta computed (e.g. a coarse rescan): rebuild from a
          # full native delta rather than the read surface.
          self._rebuild_head_graph_from_native()
          return
      self._head_graph.apply_code_delta(code_delta)   # 4.1, revision-gated inside
      self._invalidate_derived(delta)                 # unchanged (Phase 7 rewrites it)
  ```
- **Revision-gating.** Inside `apply_code_delta` (or a thin guard around it),
  compare the delta's `revision` to `self._head_graph._revision`. The normal case
  is "delta.revision is the next revision" → apply. A stale/duplicate delta
  (`<= current`) is a no-op; an out-of-order gap triggers a full rebuild from a
  fresh native delta. Because publication is the commit tail (§5.3) and all writes
  funnel through one path, in-order is the rule — gating makes the exception safe
  rather than corrupting the graph.
- **Build the head graph from the native delta too.** Change the `graph` property
  (`session.py:733`) so the first materialisation applies a **full** native code
  delta rather than `CodeGraph.build(self)`:
  ```python
  @property
  def graph(self):
      self._check_open()
      if self._head_graph is None:
          from tyo3.graph import CodeGraph
          g = CodeGraph()
          g._root = self.root.resolve()
          g.apply_code_delta(self._inner.full_code_delta())  # Phase 2 accessor
          self._head_graph = g
      return self._head_graph
  ```
  Factor the "fresh full apply" into a small `_rebuild_head_graph_from_native()`
  helper so the property and the rescan branch share it.
- `LatestView.graph()` (`session.py:1694`) already delegates to `session.graph`;
  it needs no change beyond inheriting the new, side-effect-free build.

**Verify (with the legacy build still present — A/B against parity).**
```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py src/tyo3/tests/test_graph*.py -q --no-cov -rA
```
The head graph is now produced by the native delta; parity (which builds the
legacy graph independently) is your equality check. **Drive any diff to empty
before deleting anything.**

> Commit here: `refactor(session): head graph + post-commit update driven by native code delta`.

#### (b) Delete the legacy read-surface construction

Only once (a) is green and parity holds. Delete from `src/tyo3/graph/graph.py`
(the plan enumerates exactly these):

- the **six-pass `build`** body's read-surface passes (`graph.py:158-257`) — keep
  a `CodeGraph` constructor/empty graph, but the *symbol-collection / node-
  materialisation / reference-resolution / inheritance build* is gone;
- **per-file symbol collection**: `_collect_symbols_for_file` (`graph.py:276`),
  `_materialize_file_nodes` (`graph.py:303`), `_add_containment_edges_for_file`
  (`graph.py:387`), `_build_range_cache_for_file` (`graph.py:403`);
- **occurrence-based reference resolution**: `_resolve_references_via_occurrences`
  (`graph.py:528`), `_resolve_references_via_tokens` (`graph.py:814`),
  `_resolve_import_via_goto` (`graph.py:727`), `_add_import_edge` (`graph.py:686`),
  `_ensure_target_node*` (`graph.py:774`,`:918`), `_infer_package`
  (`graph.py:953`), `_find_symbol_in_file` (`graph.py:501`), `_resolve_parent_id`
  (`graph.py:483`), `_find_enclosing_symbol` (`graph.py:1113`);
- the **inheritance/override passes**: `_inherits_pass_I` (`graph.py:978`),
  `_overrides_pass_II` (`graph.py:1048`);
- the **full rebuild + old delta-application internals**: the legacy
  `apply_delta` (`graph.py:1924`), `_index_files` (`graph.py:1319`),
  `_handle_moved_entities` (`graph.py:1819`),
  `_revalidate_inbound_inheritance` (`graph.py:1871`), `_replace_with`
  (`graph.py:1807`) if now unused, and the diagnostics-collection passes
  `_collect_all_diagnostics` / `_refresh_diagnostics` (`graph.py:425`,`:464`) if
  you re-home diagnostics (see the decision below);
- the now-dead resolution indices `_name_to_id`, `_name_prefix_index`,
  `_file_node_ranges` and any helpers that only fed them.

**Keep** the *query/projection* surface — these are why the graph still exists:
`graph` accessor (`graph.py:1446`), `symbol` (`:1472`), `symbols_in_file`
(`:1477`), `symbols_of_kind`, `references_to`/`references_from`,
`children`/`parent`/`module_for`, the dependency/cycle/reachability queries
(`graph.py:1559-1799`), `diff` (`:1302`), `_pin_at` (`:1250`), `_add_node` /
`_add_edge` / `_rebuild_indexes` (used by the applier), and `_importers_of`
(`:761`, now fed from the applier-maintained `_file_importers`).

> **Decision to record — diagnostics.** Diagnostics are *not* part of the code
> delta; the legacy build populated them by reading the analysis `check()` surface
> (`_collect_all_diagnostics`). A `check()` is a pure **read** (it does not advance
> head), so re-homing diagnostics as a separate, explicitly read-only refresh does
> **not** violate "reads don't write." Choose one and write it down: either (i)
> keep a slim `refresh_diagnostics(source)` that reads `source.check()` and is
> called by the graph builders (never by the applier), or (ii) move diagnostics
> off the graph entirely onto the session/snapshot read surface. Do **not** leave
> diagnostics being produced by a deleted build pass. The parity comparator does
> not compare diagnostics (it compares nodes/edges only), so this choice is about
> preserving `test_graph_analysis.py` behaviour, not parity.

**Verify.**
```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py src/tyo3/tests/test_graph*.py -q --no-cov -rA
```
Parity must still be green (the native path is now the *only* path that builds the
head graph; the legacy `build` is gone, so the oracle's `legacy_graph` must now
also be re-pointed — see the note below).

> **Oracle note.** `parity_oracle.legacy_graph` (`parity_oracle.py:191`) calls
> `CodeGraph.build`. Once `build` no longer does a read-surface construction, the
> oracle's two halves converge to the same path and parity becomes trivially true
> — which is fine and expected at the *end* of cutover (parity has done its job).
> Do this re-point **last**: keep a read-surface `legacy_graph` alive through 4.2(a)
> and the incremental deletions so it remains an independent reference, and only
> collapse it when the legacy build is actually deleted. If you prefer to retain an
> independent check longer, you may keep the legacy build behind a test-only flag
> until the end of the phase — but the plan's intent is deletion, so do not ship it.

> Commit here: `refactor(graph): delete read-surface build; graph is a pure native-delta applier`.

---

### Step 4.3 — Remove read-side writes

**What.** Delete the identity-priming routine and its guard flag so reading the
graph can never advance the revision.

**Why.** §5.3 ("reads do not write") / §5.9 / §6.3 defect #3. Identity is
reconciled at open (Phase 1.3) and on every commit (Phase 3), so it is **always**
populated before a graph read — the prime is now both unnecessary and a contract
violation.

**Where / how.**
- Delete `_prime_identity_registry` (`graph.py:34-45`) and its call site
  (`graph.py:184`, inside the now-rewritten `build`/constructor path).
- Delete the `_graph_identity_primed` flag set (`graph.py:43`) and any reads of
  it. Search the tree for `_graph_identity_primed` and remove all references
  (it is a session attribute set via `setattr`).
- **Reading the graph must never call `sync_all` or advance the revision.** With
  the native-delta build (4.2a), the build path no longer takes a `session` write
  surface at all — it applies `self._inner.full_code_delta()`. Make sure no
  residual path calls `session.sync_all()` during a read.
- **Typed build failure, never a repair-write.** If a graph build (head or
  snapshot) finds required identity missing — which should not happen after Phase
  1/3, but is a real defensive case — it raises a typed `GraphBuildFailure`
  (`graph/models.py`, already imported at `graph.py:18`), **not** a `sync_all` to
  populate it. Record the missing id(s) on the failure for diagnosis.

**Verify.**
```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov -rA
```
Remove the three `@pytest.mark.xfail(strict=True)` markers
(`test_final_no_read_side_writes.py:39`,`:55`,`:75`) only once each case passes on
its own (an xfail that starts passing with the marker still on is itself a failure
under `xfail-strict`). The first two cases (`session.graph`,
`snapshot().graph()`) are satisfied by 4.2a + this step; the third
(`latest.check()` / `latest.graph()`) by `LatestView.graph()` inheriting the
side-effect-free build.

> Commit here: `refactor(graph/session): remove identity priming; reads never advance head`.

---

### Step 4.4 — Build the snapshot graph from native state

**What.** `Snapshot.graph()` must apply a native code delta computed over the
**snapshot's own frozen database**, not a read-surface walk over the snapshot, so
the graph is consistent with the snapshot's pinned revision — and it must mutate
no session state.

**Why.** §5.2 / §5.9 / Phase 4.4. A snapshot is a revision-pinned, isolated read
surface; its graph must be a pure projection of *its* revision's content, built
without warming or mutating the live head and without taking the write lock.

**Where / how.**
- **Expose a full-code-delta accessor on the snapshot's native handle.** Phase 2
  added `full_code_delta()` on `PyTyProject` (the live head). Add the analogous
  method on the **snapshot/frozen** native handle (`Snapshot._inner`,
  `session.py:1396-1419`), in `rust/src/project.rs`, that runs `produce_code_delta`
  (Phase 2) over the snapshot's **frozen** `TyProjectState`/database and its pinned
  registry, returning a full (`rescan=true`, scope=all) pythonized `CodeDeltaDto`.
  This is the snapshot analogue of the head accessor; it reads only the frozen
  generation (no disk, §5.1/§5.2) and the pinned identity, and must not touch the
  live head.
- **Rewrite `Snapshot.graph()`** (`session.py:1484`):
  ```python
  def graph(self):
      """An immutable CodeGraph pinned at this snapshot's revision."""
      self._check_open()
      if self._graph is None:
          from tyo3.graph import CodeGraph
          g = CodeGraph()
          g._root = self._root
          g.apply_code_delta(self._inner.full_code_delta())  # frozen-db delta (4.4)
          self._graph = g._pin_at(self.revision)
      return self._graph
  ```
  No `CodeGraph.build(self, ...)`, no `_prime_identity_registry`, no session
  reference. `_pin_at` (`graph.py:1250`) still freezes the result to an immutable,
  revision-tagged copy.
- **Determinism / parity.** The frozen-db producer must emit the *same* node
  ids/payloads and edge multiset the live producer does for the same content, so
  the snapshot graph and a head graph at the same revision agree (and the parity
  oracle, which can be pointed at a snapshot `source`, stays green). Reuse the
  exact `produce_code_delta` path — do not write a snapshot-specific producer.

**Verify.**
```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_graph_snapshots.py src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov -rA
# Parity can be asserted against a snapshot source too:
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py -q --no-cov -rA
```
`test_snapshot_graph_does_not_advance_head` must be green with its marker removed;
`test_graph_snapshots` must still pass over the native-built pinned graph.

> Commit here: `feat(snapshot): build pinned graph from the snapshot's own native code delta`.

---

## 5. Acceptance — the Phase 4 gate

Run exactly what the plan's Phase 4 "Acceptance" lists, all via devenv:

```bash
devenv shell -- build   # ensure pytest imports the freshly built extension

devenv shell -- pytest \
  src/tyo3/tests/test_final_no_read_side_writes.py \
  src/tyo3/tests/test_graph*.py \
  -q --no-cov -rA

# the full parity suite, still green over the native-only path
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py -q --no-cov -rA
```

Then the full milestone gate (run the suites in the background; ~10–15 min):

```bash
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria (all must hold):**
- `CodeGraph.apply_code_delta` is a **pure** applier (no FFI, no read-surface
  reads, no session/snapshot access) and is the **only** mechanism that builds or
  updates a graph (head and snapshot).
- The post-commit head update and both graph builders (head + snapshot) are driven
  by the **native code delta**, revision-gated; an out-of-order apply is
  impossible.
- The **read-surface construction is gone** from `graph/graph.py`: the per-file
  symbol collection, node materialisation, occurrence-based reference resolution,
  inheritance/override passes, the full rebuild, and the old
  delta-application internals are deleted.
- **No read accessor advances head** — `session.graph`, `Snapshot.graph()`,
  `LatestView.graph()`/`check()` all leave `session.head` unchanged. Proven by
  `test_final_no_read_side_writes.py` with all three `xfail` markers removed.
- **No priming flag remains** — `_prime_identity_registry` and
  `_graph_identity_primed` are deleted; a missing-identity graph build raises a
  typed `GraphBuildFailure`, never a `sync_all`.
- The snapshot graph is built over the **snapshot's own frozen database** and
  mutates no session state.
- **Structural parity stays green** (or has been collapsed because the legacy
  build is gone); the snapshot graph and a head graph at the same revision agree
  structurally, and any cosmetic divergence is known.
- Both full suites stay green; `test_graph*` behaviour is preserved over the native
  applier.

---

## 6. Pitfalls specific to this phase

- **Deleting the legacy build before the native path is authoritative.** The
  cardinal Phase 4 mistake. Do 4.1 and 4.2(a) — and confirm parity green — *before*
  any deletion in 4.2(b). The legacy build is your reference; delete it last.
- **Forgetting `devenv shell -- build` before the parity / no-read pytest.** Phase
  4 adds a Rust accessor (4.4) and flips the authoritative path; a green
  `cargo test` does not refresh the compiled extension. If the test "won't change
  no matter what you fix," you skipped the rebuild.
- **An impure applier.** Reaching for `source.files()`, `document_symbols`, or any
  FFI inside `apply_code_delta` to "patch a parity gap" re-introduces exactly the
  coupling this phase removes. Fix the *producer* (Phase 2); the applier sees only
  the graph and the delta.
- **Stale rustworkx indices after node removal.** `remove_nodes_from` invalidates
  stored integer indices. Rebuild the secondary indices (`_rebuild_indexes`) after
  bulk removals; a dangling `_id_to_index` entry is a silent wrong-node bug the
  comparator will flag as a payload diff or a phantom edge.
- **Edges before nodes.** `_add_edge` resolves endpoints via `_id_to_index`; apply
  all `nodes_upserted` before any `edges_added`. An edge whose endpoint is absent
  is a producer bug — surface it, do not silently drop it.
- **A move leaking into create/delete at the graph level.** `nodes_moved` updates
  location only; it must not remove+re-add the node (that churns edges and breaks
  `_pin_at` stability). Honour §5.5 in the applier as the producer does in the
  delta.
- **Re-homing a read-side write somewhere else.** Removing
  `_prime_identity_registry` but then calling `sync_all` from a graph build path
  (or from diagnostics refresh) reintroduces the defect. Diagnostics `check()` is
  a *read*; `sync_all` is a *write* — never the latter on a read path.
- **Snapshot graph built over the live head.** `Snapshot.graph()` must use the
  snapshot's **frozen** native handle's `full_code_delta()`, not the session's, or
  the pinned graph silently reflects a newer revision (a §5.2/§5.9 violation that
  `test_graph_snapshots` may not catch but the acceptance intent forbids).
- **Forgetting the floating latest path.** `LatestView.graph()` delegates to
  `session.graph`; if you fix the property but a residual prime survives elsewhere,
  `test_latest_check_does_not_advance_head` stays red. Grep the whole tree for
  `sync_all` on read paths.
- **Diagnostics quietly disappearing.** Deleting `_collect_all_diagnostics`
  without re-homing the diagnostics read will regress `test_graph_analysis.py`.
  Decide where diagnostics live (the decision in 4.2b) and wire it, read-only.
- **Collapsing the oracle too early.** If you re-point `legacy_graph` to the native
  path before the deletion is actually done, parity becomes trivially true and
  stops guarding the very deletion it exists for. Collapse it only when the legacy
  build is truly gone.

---

## 7. Suggested commit sequence for the phase

1. `feat(graph): hardened pure apply_code_delta (nodes/edges/moves/indices, no FFI)` (4.1)
2. `refactor(session): head graph + post-commit update driven by native code delta` (4.2a)
3. `refactor(graph): delete read-surface build; graph is a pure native-delta applier` (4.2b)
4. `refactor(graph/session): remove identity priming; reads never advance head` (4.3)
5. `feat(snapshot): build pinned graph from the snapshot's own native code delta` (4.4)

This maps to the plan's single commit
`refactor(graph): cut over to a pure applier; remove read-surface build +
priming`; squash on landing if the series is preferred as one reviewed commit.

Every commit passes its focused `pytest` slice; the last one passes the
no-read-side-writes test, the graph suite, the parity suite, and both full suites
(the milestone gate). Remember: **all of it through `devenv shell --`.**

---

## 8. What Phase 4 deliberately leaves for later (so you don't over-reach)

- **The single staged native `commit()` with rollback** (sidecar as a participant;
  no torn publish) is **Phase 5**. Phase 4 makes the graph a pure applier of the
  committed delta; it does **not** restructure the commit transaction itself.
- **One unified Python post-commit path + non-blocking bus** is **Phase 6**. Phase
  4 leaves `_apply_graph_delta` / `_invalidate_derived` / `_publish_delta` as the
  separate per-method steps they are today (it only changes what `_apply_graph_delta`
  *does*); Phase 6 collapses them into one `_after_commit` hook and fixes the write
  path that forgets to publish.
- **Id-level derived invalidation** is **Phase 7**. Phase 4 keeps
  `_invalidate_derived` working off the delta as-is; Phase 7 rewrites it to consume
  `created_ids`/`changed_ids`/`deleted_ids` and fixes the snapshot-lifetime leak.
- **The convenience reads** (`session.code` / `session.layer` / `session.entity`
  returning views over closed snapshots, `session.py:1306-1330`) are **Phase 8**.
  Phase 4 does not touch them — leave the closed-snapshot-view fix for the read-
  surface phase.
- **Typing the nested `code_delta`** (Phase 3 left it `dict | None`), the config
  re-read, the file splits, and warning/typing hygiene are **Phases 9–12**.

Keeping these out of Phase 4 is what isolates the high-value, high-risk cutover —
making the native delta authoritative and deleting the legacy build — behind the
parity oracle, and keeps the suite green at every commit.
