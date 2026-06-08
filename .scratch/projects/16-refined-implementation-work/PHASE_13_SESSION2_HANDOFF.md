# Phase 13 — Session 2 handoff: finish 13.3 → 13.4 → closeout

You are resuming the TyO3 spine refactor **Phase 13 (Split the monoliths)** in a
fresh session. **13.1 and 13.2 are DONE and committed.** Your job is **13.3
(split `graph.py`)**, then **13.4 (confirm bus/precision)**, then the **closeout**
(PROGRESS record + defect-table flip + final milestone gate + the existing kickoff
note). This phase is **behaviour-preserving** — pure moves + re-exports, no
behaviour changes, no bug fixes. **The full suite is the guard: it must be green
after the 13.3 split.**

---

## 0. Where things stand (read this first)

- **Branch:** `spine-refactor-phase-1`. **HEAD:** `3db2428`.
- **Landed this phase (do NOT redo):**
  - `cd0162a` — **13.1**: split `rust/src/project.rs` (4656 lines) → `rust/src/project/`
    submodules: `analysis.rs` (compute_* LSP read cores), `open.rs` (open/HEAD
    construction), `commit.rs` (staged commit + producer driver), `methods.rs`
    (thin PyO3 `PyTyProject` wrappers), `snapshot.rs` (`PySnapshot`),
    `head_view.rs` (`PyHeadView`). The root `project.rs` keeps the shared state
    types (`TyProjectState`, `HeadState`, `PyTyProject` struct, lock helpers),
    re-exports the submodules via `pub(crate) use <mod>::*;`, and keeps the unit
    tests. `identity.rs`/`code_layer.rs` stayed separate. cargo test 167 passed;
    full pytest green.
  - `3db2428` — **13.2**: split `src/tyo3/session.py` (1993 lines) → `src/tyo3/session/`
    package: `_native.py` (PyO3 handle + native-exception shims), `read_ops.py`
    (`_ReadOps` shared read surface), `views.py` (`_OwnedView`/`Snapshot`/`LatestView`),
    `session.py` (`TyO3Session` facade), `__init__.py` (re-exports). Public surface
    unchanged. Full pytest green.
- **Baseline before this phase:** `6341805` (Phase 12). Going-in state is a fully
  green `pytest -q --no-cov` (exit 0, zero new xfail/XPASS) + green `cargo test`.
  **Do not regress it.**
- **Working tree:** clean except one untracked planning note
  `.scratch/projects/16-refined-implementation-work/PHASE_13_KICKOFF.md` (the
  original kickoff — leave it; commit it at closeout or with 13.4).

## Non-negotiable working rules (every step)

- **Run EVERYTHING through `devenv shell --`** (Nix). Never bare `pytest`/`cargo`/`ruff`.
- **13.3 and 13.4 are Python-only → NO Rust rebuild needed.** (Only rebuild after a
  Rust change; there are none left in Phase 13.)
- Suites are slow (~10–15 min). **Run the full suite in the background**, then wait
  on the completion notification (don't sleep-poll; use `run_in_background: true`).
- **Diff carefully: no behaviour change may sneak into a "pure move."** If you find
  yourself fixing a bug or changing a signature, STOP — not this phase.
- **No AI attribution** in commits/PRs/docs (house rule — no Co-Authored-By / "Generated with").
- Commit style for this phase: **one commit per split** (13.1 and 13.2 already are).

## The proven technique (used for 13.1 and 13.2 — reuse it)

1. **Carve with `sed -n 'A,Bp'`** into the new files (Edit/Write can't move large
   blocks; sed line-range extraction is the right tool — it's verified-necessary
   here, not a shortcut). Prepend each file's import/`use` header, append the body.
2. **Verify imports before running the slow suite:**
   - Rust: `devenv shell -- cargo check --manifest-path rust/Cargo.toml` (fast,
     incremental; the compiler is exhaustive for visibility/resolution).
   - Python: `devenv shell -- ruff check --select F src/tyo3/graph/` — **F401**
     (unused import), **F811** (redefinition), **F821** (undefined name) are your
     correctness net for a mechanical import split. `ruff 0.15.15` is available.
3. **Import smoke + cycle check** (Python):
   `devenv shell -- python -c "import tyo3; import tyo3.graph; import tyo3.session; from tyo3.graph import CodeGraph; print('ok')"`
4. **Then the slow gate** (background): full `pytest`.
- **Line-number caveat:** adding/removing lines shifts every line below. Re-derive
  anchors with `grep -n` after any edit that changes line count before the next
  sed by line range. (In 13.1 a header edit added 5 lines and everything below
  shifted +5 — re-grepped to confirm.)

---

## 1. STEP 13.3 — Split `src/tyo3/graph/graph.py` (1082 lines) → `projection.py` + mixins

### Decisions (already made — keep them)

- **Rename `graph.py` → `projection.py`.** The class is now a **pure projection**
  of `full_code_delta()`, not a transaction participant; `projection.py` signals
  that unambiguously (chosen over `snapshot_graph`, which collides with the
  `Snapshot` concept). **Keep the public symbol `CodeGraph`**, re-exported from
  `tyo3/graph/__init__.py` so `from tyo3.graph import CodeGraph` is unchanged.
- **Split the single `CodeGraph` class via mixins** — the standard Python way to
  spread one class's methods across files. `CodeGraph` (in `projection.py`)
  inherits four mixins; **all instance attributes are initialized in
  `CodeGraph.__init__` (stays in projection.py); the mixins define NO `__init__`.**
  Cross-mixin `self.X(...)` calls resolve via MRO at runtime — **no imports needed
  between mixins.**
- **No runtime cycle:** mixins must **not** import `projection.py` at runtime. They
  reference `CodeGraph` only in annotations (strings — `from __future__ import
  annotations` is already at the top of the file, line 3) and under `TYPE_CHECKING`.
  `projection.py` imports the four mixins to compose `CodeGraph`; the edge is
  one-way (projection → mixins, mixins → `_helpers`/models leaves).

### Target modules under `src/tyo3/graph/` (`export.py`/`models.py` already exist — leave them)

| New module | Class / contents |
|---|---|
| `_helpers.py` (leaf) | module fns `_to_relative`, `_normalize_result_path`, `_ranges_overlap` |
| `applier.py` | `class _ApplierMixin` |
| `queries.py` | `class _QueriesMixin` + module const `DEPENDENCY_EDGE_KINDS` |
| `diff.py` | `class _DiffMixin` |
| `diagnostics.py` | `class _DiagnosticsMixin` + module `logger` |
| `projection.py` (renamed graph.py) | `class CodeGraph(_ApplierMixin, _QueriesMixin, _DiffMixin, _DiagnosticsMixin)` core |

### Exact method → module map (current `graph.py` line numbers — file is UNCHANGED, so they hold)

Use these to carve. Each method/fn block runs from its `def`/`@` line to just
before the next one listed. **Re-confirm boundaries with `grep -nE "^(class |    def |    @|def )" src/tyo3/graph/graph.py` before cutting.**

**`_helpers.py`** (module-level fns; leaf — imports `Path`, `PurePosixPath`, and
`Range` from `tyo3.models.analysis`):
- `_to_relative` (33–46), `_normalize_result_path` (48–58), `_ranges_overlap` (1076–end)

**`projection.py` — `CodeGraph` core** (construction / lifecycle / MVCC / properties):
- `__init__` (77–110), `build` classmethod (113–156), `build_with_report` (183–198),
  `_rebuild_indexes` (288–314), `_assert_mutable` (315–319), `_pin_at` (320–336),
  properties `graph` (420–426), `revision` (427–431), `frozen` (432–436),
  `node_count` (437–440), `edge_count` (441–445)

**`applier.py` — `_ApplierMixin`** (the projection applier — code-delta → graph mutation):
- `_add_node` (259–269), `_add_edge` (270–287), `_coerce_range` staticmethod (784–791),
  `_node_from_code_delta` (792–807), `apply_code_delta` (808–909),
  `_add_code_edge` (910–932), `_remove_code_edge` (933–976)

**`queries.py` — `_QueriesMixin`** (read queries over the projection):
- `DEPENDENCY_EDGE_KINDS` const (61–71), `_importers_of` (242–258),
  `symbol`→`subgraph_for_file` contiguous (446–783: symbol, symbols_in_file,
  symbols_of_kind, references_to, references_from, children, parent, module_for,
  `_semantic_subgraph`, dependencies, dependents, transitive_dependencies,
  transitive_dependents, `_build_module_graph`, import_cycles, hub_symbols,
  import_cycle_groups, is_reachable, has_cycles, topological_order,
  subgraph_for_file), `_has_import_edge_between` (977–987), `coupling_between`
  (988–1013), `external_symbols` (1014–1022), `external_symbols_by_package`
  (1023–1032), `resolve_external` (1033–1058)

**`diff.py` — `_DiffMixin`** (contiguous 337–419):
- `_range_key` staticmethod (337–346), `_edge_map` (347–368), `diff` (369–385),
  `_edges_of_kind` (386–419)

**`diagnostics.py` — `_DiagnosticsMixin`** (+ `logger = logging.getLogger(__name__)`):
- `refresh_diagnostics` (158–182), `_collect_all_diagnostics` (199–241),
  `diagnostics_for_file` (1059–1062), `diagnostics_for_symbol` (1063–1070),
  `all_diagnostics` (1071–1075)

> **Interleaving note:** lines 158–336 mix concerns — route those nine methods
> individually per the map above (refresh_diagnostics→diag, build_with_report→core,
> _collect_all_diagnostics→diag, _importers_of→queries, _add_node/_add_edge→applier,
> _rebuild_indexes/_assert_mutable/_pin_at→core). Everything from 337 on is in
> large contiguous concern-blocks (easy cuts).

### Cross-reference facts (already mapped — so you place helpers/imports right)

- `logger` is used **only** at line 218 (in `refresh_diagnostics`) → define a module
  `logger` in `diagnostics.py`.
- `_to_relative` callers: 55 (`_normalize_result_path`, same `_helpers`), 175
  (`build_with_report`→projection), 232 (`_collect_all_diagnostics`→diagnostics) →
  import `_to_relative` from `_helpers` in `projection.py` and `diagnostics.py`.
- `_normalize_result_path` caller: 232 (diagnostics) → import in `diagnostics.py`.
- `_ranges_overlap` caller: 1069 (`diagnostics_for_symbol`→diagnostics) → import in `diagnostics.py`.
- `DEPENDENCY_EDGE_KINDS` callers: 540/550/563/579 (all in `queries`) → keep the
  const **in `queries.py`** (queries-local).
- `_range_key` caller: 360 (`_edge_map`→diff) → staticmethod on `_DiffMixin`.
- `_coerce_range` callers: 800/801/890/891/915/945 (all in `applier`) → staticmethod on `_ApplierMixin`.
- `_add_node`/`_add_edge` callers: applier only (`apply`/`_add_code_edge`) → on `_ApplierMixin`.
  They call `self._assert_mutable()` (in core) — fine via MRO.
- `_importers_of`, `_has_import_edge_between`, `external_*`, `resolve_external` are
  query helpers → `_QueriesMixin`.

### Imports per mixin — the safe mechanical way (same as 13.2)

Give each new module a **copy of `graph.py`'s import block** (lines 1–30: `logging`,
`defaultdict`, `Path`/`PurePosixPath`, `TYPE_CHECKING`/`Any`, `rustworkx as rx`,
`DependencyGraph`, identity helpers, `tyo3.graph.models` symbols,
`tyo3.models.analysis` `Diagnostic, Range`, `tyo3.models.navigation ReferenceRole`,
`tyo3.models.symbols SymbolKind`, the `TYPE_CHECKING: from tyo3.session import
TyO3Session` block), plus its cross-module needs (`from tyo3.graph._helpers import …`;
`projection.py` also does `from tyo3.graph.applier import _ApplierMixin`, etc.).
Then **trim with `devenv shell -- ruff check --select F401 src/tyo3/graph/ --fix`**
(removes only provably-unused imports — safe; F-checks don't touch annotation-only
names because `from __future__ import annotations` keeps pyflakes aware of them).
Then `--select F811,F821` to confirm no redefinition / undefined name.

### Importers to repoint (the only files that deep-import `tyo3.graph.graph`)

- `src/tyo3/graph/__init__.py:7` — `from tyo3.graph.graph import CodeGraph`
  → `from tyo3.graph.projection import CodeGraph`. (Everything else in `__init__`
  stays; this keeps `from tyo3.graph import CodeGraph` working.)
- `src/tyo3/tests/parity_oracle.py:68` — `from tyo3.graph.graph import CodeGraph`
  → `from tyo3.graph.projection import CodeGraph`.
- `src/tyo3/tests/test_graph_apply_code_delta.py:18` — same change.
- (`parity_oracle.py:46` has a docstring `:class:~tyo3.graph.graph.CodeGraph` — a
  cosmetic ref; update or leave.)
- **Swap mechanics:** `rm -f src/tyo3/graph/graph.py src/tyo3/graph/__pycache__/graph.cpython-*.pyc`
  after creating `projection.py` (the renamed-with-mixins-extracted file). Do NOT
  leave both `graph.py` and the new modules.

### 13.3 gate (Python-only — NO rebuild)

```
devenv shell -- ruff check --select F src/tyo3/graph/        # F401/F811/F821 clean
devenv shell -- python -c "import tyo3; import tyo3.graph; import tyo3.session; from tyo3.graph import CodeGraph; print('ok')"
devenv shell -- pytest src/tyo3/graph/tests/ -q --no-cov     # fast graph subset first
devenv shell -- pytest -q --no-cov                           # FULL suite (background); must be green
```

Then **commit**: `refactor(graph): split graph.py into projection + mixins`
(behaviour-preserving; CodeGraph re-exported; no AI attribution).

### 13.3 pitfalls

- A method landing in the wrong mixin still *works* (MRO), but put it where the map
  says so the module responsibility is honest.
- Don't forget `_ranges_overlap`/`_to_relative`/`_normalize_result_path` imports in
  `diagnostics.py` (F821 will catch a miss).
- `build()`/`build_with_report()` live in `projection.py` and call
  `self.apply_code_delta` / `self.refresh_diagnostics` (other mixins) — fine via MRO.
- Keep `CodeGraph.__init__` the sole initializer of all `self._*` attributes.

---

## 2. STEP 13.4 — Confirm bus/precision boundaries (CONFIRM-ONLY)

Already verified in session 1 — **no work expected, just re-confirm**:
- `src/tyo3/bus/` is already split: `bus.py` / `subscription.py` / `delta.py` /
  `interest.py` / `refinement.py`; internal cross-refs are all within `tyo3.bus.*`
  and mostly `TYPE_CHECKING`-guarded.
- `src/tyo3/precision/refiner.py` imports only `tyo3.bus.refinement` (one-way).
- **Neither `bus/` nor `precision/` imports `session` or `graph`** → no cycle.
  Re-check: `grep -rn "tyo3.session\|tyo3.graph" src/tyo3/bus/ src/tyo3/precision/`
  should return nothing. Fix only if a cycle actually exists (it doesn't).

13.4 gate: the full milestone gate (below). No separate commit needed unless you
prefer one (`chore(bus,precision): confirm split boundaries` is optional).

---

## 3. CLOSEOUT (after 13.3 + 13.4 green)

### 3a. Final milestone gate
```
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```
(Rust unchanged since 13.1, but run cargo test once more to certify the phase.)
Exit criteria: full suite green; public imports unchanged
(`from tyo3 import TyO3Session`, `from tyo3.graph import CodeGraph`,
`from tyo3.exceptions import …`); each module single-responsibility; no cycles;
graph module renamed to `projection`.

### 3b. PROGRESS.md updates (`.scratch/projects/16-refined-implementation-work/PROGRESS.md`)
- Add a **`### §9.13 — V2 Phase 13: split the monoliths ✅ DONE (2026-06-08)`**
  record alongside §9.12 (~line 589). Cover: the three splits, the
  **decisions** (Rust = free-fns-in-submodules + thin `#[pymethods]`, multiple-pymethods
  enabled so the `PyTyProject` method block lives in `methods.rs`; graph renamed to
  `projection` via mixins; session layered facade→views→read_ops→_native), the
  **pre-existing leftovers** (the un-imported `Optional` annotation in the session
  facade and the `dto/mod.rs` `sync::*` + cfg(test)-gated `Document`/`file_name`
  warnings — all PRE-EXISTING, deferred to Phase 14), and the gate results. Link
  `[[phase12-...]]` neighbors as the file's style does.
- Flip the **§6.3 defect table row #10** (~line 676) from
  `🔴 **V2 Phase 13**` to ✅ **DONE** with a one-line note.

### 3c. Memory pointer
Add a one-line pointer in
`/home/andrew/.claude/projects/-home-andrew-Documents-Projects-tyo3/memory/MEMORY.md`
under the Pointers list (and a small memory file if you write one), e.g.
`- [Phase 13 monolith split done](phase13-monolith-split-done.md) — project.rs→project/ submodules, session.py→session/ package, graph.py→projection.py+mixins; behaviour-preserving; next is Phase 14 (warnings/typing/hygiene + acceptance)`.

### 3d. Commit the planning notes
The kickoff (`PHASE_13_KICKOFF.md`) and this handoff are untracked; commit them with
the PROGRESS update (e.g. `docs(phase13): progress record + defect table; close deviation #10`).

---

## 4. Quick reference

- Project memory says: **always `devenv shell --`**; test scripts `tests`,
  `test-fast`, `test-rust`, etc.; build scripts `build`, `build-release`. Suites
  are slow — give ~15 min, run in background.
- `multiple-pymethods` IS enabled in `rust/Cargo.toml` (relevant only to 13.1, done).
- The phase adds **no new tests** — the existing suite IS the acceptance.
- Reference docs: `.scratch/projects/15-implementation-plan/PHASE_13_IMPLEMENTATION_GUIDE.md`
  (steps 13.1–13.4), `REFINED_IMPLEMENTATION_PLAN_V2.md` (Phase 13 ~line 189),
  `REFINED_IMPLEMENTATION_CONCEPT.md` §6.3 deviation #10 (~line 454),
  `START_HERE_V2.md` (V2 numbering: Phase 13 = V1 "Phase 11").

**Start by:** confirming HEAD is `3db2428` and the tree is clean, re-grepping
`graph.py`'s method boundaries (`grep -nE "^(class |    def |    @|def |[A-Z_]+ =)" src/tyo3/graph/graph.py`)
to validate the line map above, then executing 13.3.
