We're continuing the TyO3 spine refactor. All planning is done and lives in
`.scratch/projects/15-implementation-plan/`. Your job this session is to implement
**Phase 13: Split the monoliths** — break the three central files
(`rust/src/project.rs`, `src/tyo3/session.py`, `src/tyo3/graph/graph.py`) into
focused, single-responsibility modules so the structure becomes reviewable and
ownership stops hiding (V1 §6.3 deviation #10). This is the V1 "Phase 11" work.

**This phase is BEHAVIOUR-PRESERVING.** No behaviour changes, no new features,
no bug fixes. Every split is a pure move + re-export. **The full suite is the
guard: it must be green after EACH split, not just at the end.** Split one file
at a time and gate between. If you touch Rust, `devenv shell -- build` before any
pytest gate (the #1 phantom-failure source is testing a stale extension).

Before writing any code, read in this order:
  1. `.scratch/projects/15-implementation-plan/START_HERE_V2.md`        (orientation — the V2 phase-numbering table; Phase 13 = V1 "Phase 11")
  2. `REFINED_IMPLEMENTATION_CONCEPT.md` (V1) §6.3 deviation #10        (the exact defect — three monoliths mix responsibilities)
  3. `REFINED_IMPLEMENTATION_PLAN_V2.md`  (Phase 13 section, ~line 189) (the split map + acceptance)
  4. `PHASE_13_IMPLEMENTATION_GUIDE.md`                                 (the step-by-step 13.1–13.4 you'll execute)
Then skim, for current ground truth:
  5. `.scratch/projects/16-refined-implementation-work/PROGRESS.md` §9.12 (Phase 12, just done) and §6.3 defect table (row #10 is what this phase closes)

## Critical context — Phase 12 is LANDED; the seams are already cut for you

- **Phase 12 just landed (commit `6341805`)**: single validated config source —
  Python consumes the native config via `TyConfig.coordination`; the silently-
  falling-back `_read_coordination_config` re-read is deleted. **The baseline going
  into Phase 13 is a fully green `pytest -q --no-cov` (exit 0, zero new
  xfail/XPASS) + green `cargo test`.** Don't reintroduce a failure; this phase
  adds no tests of its own — the existing suite IS the acceptance.
- **The V2 refactor already made the write methods thin and the graph a pure
  projection** — those are exactly the seams to cut along. The write methods are
  `native op → validate → _after_commit → return`; `graph` is a projection of
  `full_code_delta()`, not a transaction participant. You are *separating along
  existing seams*, not redesigning.
- **`bus/` and `precision/` are already split** (verify, don't rebuild): `bus/`
  has `bus.py` / `subscription.py` / `delta.py` / `interest.py` / `refinement.py`;
  `precision/` has `refiner.py`. So **13.4 is "confirm clean boundaries / no
  import cycles," not new machinery.**

## The monoliths and their current size (the targets)

  - `rust/src/project.rs`     — **4656 lines** (by far the biggest; 13.1)
  - `src/tyo3/session.py`     — **1993 lines** (13.2)
  - `src/tyo3/graph/graph.py` — **1082 lines** (13.3)

Current Rust modules already separate `identity.rs`, `code_layer.rs`, `config.rs`,
`authored.rs`, `content.rs`, `overlay.rs`, `files.rs`, `entity.rs`, `hash.rs`,
`sidecar.rs`, `coordinates.rs` + `convert/` + `dto/` — so `project.rs` is what's
left to carve. The graph package already has `dependency.py`, `export.py`,
`identity.py`, `models.py` alongside `graph.py`.

## What Phase 13 changes — the four steps (gate between each)

- **13.1 — Split `rust/src/project.rs`** into focused modules: `open`,
  `head_state`, `commit` (the Phase-5 staged transaction + the Phase-6 producer
  driver), `snapshot`, `watch`, `authored`, and the **thin PyO3 method wrappers**
  (wrappers parse args, call core, convert errors). `identity.rs` and
  `code_layer.rs` **stay** in their own modules — don't fold them in.
  *Verify:* `devenv shell -- build && devenv shell -- cargo test --manifest-path rust/Cargo.toml`
- **13.2 — Split `src/tyo3/session.py`** into facade / snapshot+latest views /
  the `_after_commit` post-commit hook / shared read wrappers / public exceptions.
  **Move protocols and models out of implementation modules to break cycles.** The
  public `TyO3Session` stays a **thin facade** re-exporting the same surface — and
  `from tyo3 import TyO3Session` must keep working unchanged.
  *Verify:* `devenv shell -- pytest -q --no-cov`
- **13.3 — Split `src/tyo3/graph/graph.py`** into applier / queries / diff /
  diagnostics / export / models. Since the graph is now a **projection** (not a
  transaction participant), **name it to reflect that** (e.g. a
  `projection`/`snapshot_graph` name) so its role is unambiguous. Keep
  `from tyo3.graph import CodeGraph` working (re-export from the package
  `__init__`).
  *Verify:* `devenv shell -- pytest src/tyo3/graph/tests/ -q --no-cov`
- **13.4 — Confirm the V2 additions' boundaries (mostly confirm).** `bus/` and
  `precision/` each have one clear responsibility and no cross-imports that
  recreate a cycle. They already look split — verify, fix only if a cycle exists.
  *Verify:* `devenv shell -- pytest -q --no-cov`

## The decisions you must make and state

- **Rust module layout (13.1):** free functions in submodules + a thin
  `#[pymethods] impl PyTyProject` that delegates, **vs.** `impl` blocks split
  across files via multiple `impl PyTyProject` in submodules. Let the existing
  Rust convention (`identity.rs`/`code_layer.rs` shape) and what keeps the PyO3
  wrappers thinnest drive it. Justify in the PROGRESS record.
- **Graph projection naming (13.3):** the new name for the (former) "graph"
  module that signals "projection, not transaction participant." Pick one, keep
  the public `CodeGraph` symbol re-exported, justify it.
- **Where protocols/models live (13.2):** what moves out of `session.py` /
  `graph.py` into a models/protocols module to break import cycles.

## Working rules (every phase)

- Run EVERYTHING through `devenv shell --` (Nix toolchain). Never bare `pytest`/`cargo`.
- **If you touch Rust, `devenv shell -- build` before any pytest gate.** 13.1 is a
  Rust move → rebuild before its cargo/pytest gate.
- Suites are slow (~10–15 min); run the full suite in the background.
- **Split ONE file at a time and gate between** — a green full suite after each
  split is the contract. Don't batch three splits then test once.
- **Diff carefully: no behaviour change may sneak into a "pure move."** If you
  find yourself fixing a bug or changing a signature, stop — that's not this phase.
- Don't regress the Phase-12 baseline (fully green, exit 0, zero xfail/XPASS).

## Things to verify before relying on them

- **Public import surface is unchanged.** `from tyo3 import TyO3Session`,
  `from tyo3.graph import CodeGraph`, `from tyo3.exceptions import ...` — grep the
  test suite for the import paths it uses and keep every one working via package
  `__init__` re-exports.
- **No new import cycle.** After 13.2/13.3, check that
  `python -c "import tyo3; import tyo3.graph; import tyo3.session"` (via devenv)
  imports cleanly — a cycle from leaving protocols/models in implementation
  modules is the named pitfall.
- **`bus/` and `precision/` are genuinely cycle-free** — they look split already;
  confirm 13.4 needs nothing rather than assuming it.

## The milestone gate after the phase

```bash
devenv shell -- build
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria:** full suite green after EACH split (not just at the end); public
imports unchanged (`from tyo3 import TyO3Session`, `from tyo3.graph import
CodeGraph`, etc. all work); each new module has a single clear responsibility; no
import cycles; the graph module is renamed to signal "projection." No behaviour
change — the existing suite is the proof.

Track progress in `.scratch/projects/16-refined-implementation-work/PROGRESS.md`
(add a `§9.13 — V2 Phase 13` record alongside §9.12). Update the §6.3 defect table
row #10 to DONE. Commit at the end (or per-split if you prefer a clean series)
with `refactor: split project / session / graph monoliths` (no AI attribution —
house rule).

## Start here

Read the docs above (incl. PROGRESS §9.12 and the §6.3 defect table), open the
three monolith files to map their current sections to the target split, **confirm
`bus/` and `precision/` are already cleanly split**, then give me a short Phase 13
implementation plan: the concrete module list for each of 13.1/13.2/13.3 mapped to
the real functions/sections you'll move, your **Rust-layout** and **graph-naming**
decisions (grounded in the existing conventions), the protocols/models you'll
relocate to break cycles, and a confirmation that 13.4 is confirm-only (or exactly
what cycle needs breaking if not). Note that each step is gated by a green suite
and that 13.1 requires a rebuild.
