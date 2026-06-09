# Phase 14 (V2) kickoff — Zero warnings, typing, hygiene + end-to-end acceptance

You are starting the **final phase** of the TyO3 spine refactor: **V2 Phase 14**.
It merges V1 "Phase 12 (warnings/typing/hygiene)" and V1 "Phase 13 (end-to-end
acceptance)" into the closing gate of the whole refactor, extended to assert the
V2 affected model and the optional precision refinement. **Depends on Phases 1–13
(all landed).**

Unlike Phase 13 (pure behaviour-preserving moves), Phase 14 has **two different
kinds of work** and you must not blur them:
- **Hygiene (14.1–14.3): behaviour-preserving cleanup** — lints to zero, typing,
  exception hygiene. No behaviour change. But it is **not** all mechanical: some
  findings are real bugs or test gaps (see the inventory). Treat `--fix` as a
  first pass, then **read every remaining finding and decide**.
- **Acceptance (14.4): new behaviour-asserting test** — the end-to-end proof of
  the V2 story. This is the one place you write new assertions.

---

## 0. Where things stand (read first)

- **Branch:** `spine-refactor-phase-1`. **HEAD:** `569dbd5` (Phase 13 docs commit;
  `2945333` is the 13.3 graph split). Tree clean.
- **Phase 13 just landed** (deviation #10 closed): `project.rs`→`project/`
  submodules, `session.py`→`session/` package, `graph/graph.py`→`projection.py`
  + four mixins. Full `pytest -q --no-cov` green (exit 0); `cargo test` 167/0.
- **Going-in baseline is green tests but DIRTY lints** — that is exactly what
  Phase 14 fixes. Do not mistake a lint finding for a regression you caused.

### Known pre-existing leftovers explicitly deferred here from Phase 13
- The un-imported `Optional` annotation in the session facade
  (`src/tyo3/session/session.py`).
- `rust/src/dto/mod.rs` `sync::*` glob + `cfg(test)`-gated `Document` / `file_name`
  warnings.
- `F841` "unused `sync`" lints in `src/tyo3/tests/test_incremental_parity.py`
  (these may be **missing assertions**, not dead bindings — see 14.3 note).

---

## 1. Non-negotiable working rules

- **Run EVERYTHING through `devenv shell --`** (Nix). Never bare
  `cargo`/`clippy`/`ruff`/`pytest`.
- Suites are slow (~10–15 min). **Run the full suite in the background**
  (`run_in_background: true`) and wait on the completion notification; don't
  sleep-poll.
- **Rust changes (14.1, parts of 14.2) require a rebuild** before pytest:
  `devenv shell -- build`. Python-only changes do not.
- **No AI attribution** in commits/PRs/docs (house rule — no Co-Authored-By /
  "Generated with").
- **Every deliberate swallow / every `#[allow(...)]` gets a one-line justifying
  comment.** No bare allows.
- **The acceptance test must be deterministic** — fixed content + fixed sidecar ⇒
  reproducible ids/hashes/delta. A flaky acceptance test (iteration order,
  timestamps) is a defect, not a pass.

### Reference docs
- `.scratch/projects/15-implementation-plan/PHASE_14_IMPLEMENTATION_GUIDE.md`
  (steps 14.1–14.4; the acceptance lifecycle + assertion list).
- `REFINED_IMPLEMENTATION_PLAN_V2.md` (Phase 14 ~line 204).
- `REFINED_IMPLEMENTATION_CONCEPT_V2.md` §9 (the Definition-of-Done exit criteria).
- `START_HERE_V2.md` (V2 numbering: Phase 14 = V1 "Phase 12" + "Phase 13").

---

## 2. The lint baseline you inherit (measured 2026-06-08, post-13.3)

**This is the work inventory.** Re-measure at the start (`--statistics`) to confirm
the counts before you touch anything.

### Python — `devenv shell -- ruff check src --statistics` → **113 errors** (78 auto-fixable)
Production code only (`--exclude 'src/tyo3/tests'`) → **61 errors**. Breakdown:

| Rule | All src | Notes / how to treat |
|---|---|---|
| `I001` unsorted-imports | 26 | mechanical — `--fix` |
| `UP037` quoted-annotation | 23 | mechanical — `--fix` (drop quotes; `from __future__ import annotations` is in place) |
| `F841` unused-variable | 22 | **JUDGMENT** — in tests often a *missing assertion* (`sync = s.edit(...)` never asserted). Decide: assert on it, or `_`-prefix, or delete. Not blanket-delete. |
| `F401` unused-import | 21 | mostly mechanical `--fix`, but confirm not a re-export |
| `F821` undefined-name | 6 (4 in prod) | **REAL BUG RISK** — incl. `Undefined name \`Delta\``. Investigate each: missing import vs genuine bug. Do **not** silence. |
| `UP035` deprecated-import | 5 | mechanical — `--fix` |
| `E501` line-too-long | 3 | judgment (wrap) |
| `B007` unused-loop-var, `B018` useless-expr, `B905` zip-no-strict, `E401`, `E741` ambiguous-name, `F811`, `UP012` | 1 each | read each; `B018`/`E741`/`B905` need a human call |

`devenv shell -- ruff format --check src` → **56 files would reformat** (mostly
tests). Run `ruff format src` as a final pass **after** the substantive fixes so
formatting doesn't churn the diff mid-review.

### Rust — `devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets`
- lib: **61 warnings** (47 duplicates → ~14 unique)
- lib test: **75 warnings**
- build script: **2 warnings** (`stripping a prefix manually` in `rust/build.rs` —
  use `strip_prefix`)

Top categories: needless borrow (`&` immediately dereferenced) ×13;
"this X can be simplified" ×11; doc-list-without-indentation ×10; **dead code**
(`method/function/field/variant never used/read/constructed`) ~20; unused imports
×5; unused variables ×4; too-many-arguments (8/7, 10/7, 12/7, 13/7) several;
manual `strip_prefix` ×2; redundant closure ×2; confusing/elided lifetimes ×2.

> **Clippy autofix is a first pass, not the answer.** `cargo clippy --fix` handles
> borrows/simplifications/closures. The **dead-code** warnings are decisions:
> delete genuinely-dead code, or — if it's intentional public/API surface or
> test-only — `#[allow(dead_code)]` **with a one-line reason**. The
> `too-many-arguments` ones: prefer a small params struct over a blanket allow.
> Do not blanket-`#[allow]` to make the gate pass.

---

## 3. Step-by-step

### Step 14.1 — Zero Rust warnings (rebuild after)
1. `cargo clippy --fix` for the mechanical class (commit that pass alone so the
   review is legible), then hand-resolve dead code / arg-count / lifetimes per the
   rules above.
2. Fix `rust/build.rs` manual prefix strip.
3. Clear the deferred `dto/mod.rs` glob + cfg(test) warnings.
4. Add the deny-gate to `devenv.nix` (a `clippy` script already exists — make it
   `--all-targets -- -D warnings`, or add `check-rust` to the gate).
- **Verify:** `devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings` (clean), then `devenv shell -- build` and `devenv shell -- cargo test --manifest-path rust/Cargo.toml`.

### Step 14.2 — Typing
- Reduce untyped values in the session, the native type stubs, and stores/
  generators; add **typed models for native return payloads** so wrappers validate
  less by hand. Replace mutable default arguments/fields with factories
  (`field(default_factory=...)`).
- Behaviour-preserving — full suite must stay green.

### Step 14.3 — Exception hygiene
- Replace every broad `except Exception: pass` with a **typed catch** that either
  yields a typed absence, logs with context, or re-raises a domain error. Each
  surviving deliberate swallow gets a justifying comment.
- **The `F841` test findings are part of this step's judgment, not a sweep.** A
  `sync = s.edit(...)` whose result is never asserted is usually a **gap in the
  test's intent** — prefer adding the missing assertion over `_`-prefixing. Decide
  per case; note any you intentionally leave.
- **Verify:** `devenv shell -- ruff check src` (clean) and
  `devenv shell -- ruff format --check src` (clean) — run `ruff format src` last.

### Step 14.4 — The end-to-end acceptance test (new)
Create `src/tyo3/tests/test_final_acceptance.py` exercising one project end to end:
open → pin an initial snapshot → author intent on an entity → read the code graph
from a pinned snapshot → configure a deterministic derived layer (one **local**,
one **semantic**) → edit one entity → move another unchanged → delete one → read
old and new snapshots → diff them → verify bus notifications → (with
`precision=method`) verify a refinement narrows the affected set → close & reopen →
verify identity + authored records survive → delete the derived cache & verify
recompute → assert no source files were modified.

**Assertions (the V2 story — these are the point):**
- same revision ⇒ same content; **durable ids survive a cosmetic edit and a move**;
  content hash changes **only** on a meaningful edit;
- **`affected_ids` is the transitive container-granular closure** — a base-class
  edit reports its subclasses **and their importers**; never seeds-only;
- derived artifacts keyed by content hash; the **local** layer does **not**
  recompute on a dependency-only change; the **semantic** layer **does**;
- authored records present / needs-review / orphaned as appropriate;
- **bus deltas ordered and id-level**; with `precision=method`, a refinement for
  the changed symbol **narrows** the affected set and arrives on the **refinement
  channel**;
- the snapshot diff **agrees with an independent rebuild**; **no read accessor
  advanced head**.
- **Verify:** `devenv shell -- pytest src/tyo3/tests/test_final_acceptance.py -q --no-cov -rA`

> Useful existing oracles to lean on: `src/tyo3/tests/parity_oracle.py` (independent
> rebuild vs delta), the gate suites (`test_gate5_derived`, `test_gate6_authored`,
> `test_gate7_read_surface`, `test_gate8_bus`), and `test_precision_refinement.py`
> for the `precision=method` channel shape. Pin content + sidecar for determinism.

---

## 4. The final gate (Definition of Done — Concept V2 §9)

```bash
devenv shell -- pytest -q                                                          # no unexpected skips
devenv shell -- cargo test --manifest-path rust/Cargo.toml
devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
devenv shell -- ruff check src
devenv shell -- ruff format --check src
```

Exit criteria (all must hold):
- No snapshot construction reads live disk; no read advances head.
- Every write is one native commit → one id-level commit delta → one shared
  post-commit path; failed writes fully roll back; no torn publish.
- `affected_ids` is transitive at the source (container-granular, never-miss),
  proven natively — not by a Python walk.
- The bus delta is a pure projection; no read-surface builder; no Option-B bridge.
- Derived invalidation is one affected-driven loop with per-layer key locality.
- The async precision layer narrows correctly (`method`) and degrades gracefully;
  with `container` it does not run and the system is fully correct.
- Hashing is AST-canonical; the container-subsumes-members invariant is tested.
- **All lints clean**; the end-to-end acceptance test proves the whole story.

---

## 5. Closeout (after the final gate is green)

- **PROGRESS.md** (`.scratch/projects/16-refined-implementation-work/PROGRESS.md`):
  add a `### §9.14 — V2 Phase 14: zero warnings + end-to-end acceptance ✅ DONE`
  record next to §9.13; record the before/after lint counts, the typing/exception
  decisions, the acceptance test's asserted invariants, and the final gate output.
  This is the **last** phase — note the refactor is **complete** and link the
  `[[phase13-monolith-split-done]]` neighbour.
- **Mark the V2 plan complete** in `REFINED_IMPLEMENTATION_PLAN_V2.md` /
  `START_HERE_V2.md` (Phase 14 → DONE; whole refactor closed).
- **Memory:** add a pointer in
  `/home/andrew/.claude/projects/-home-andrew-Documents-Projects-tyo3/memory/MEMORY.md`
  (and a small memory file `phase14-acceptance-done.md`), e.g.
  `- [Phase 14 done — refactor complete](phase14-acceptance-done.md) — zero warnings/typing/hygiene + end-to-end acceptance; spine refactor V2 closed`.
- Commit hygiene and the acceptance test logically (e.g. one commit for the Rust
  warnings autofix pass, one for hand-resolved dead code/typing/exceptions, one
  `test: end-to-end acceptance suite`, one `docs(phase14): progress + close V2`).
  Commit this kickoff note with the docs.

---

## 6. Pitfalls

- **Treating hygiene as a blind `--fix`.** `F821` (`Undefined name Delta`),
  `F841` in tests, `B018`/`E741`, and clippy dead-code are **decisions**, some of
  them real bugs or test gaps. Read each; fix the cause, don't silence the symptom.
- **A flaky acceptance test** from non-determinism (iteration order, timestamps,
  unpinned sidecar). Pin everything; ids/hashes must be reproducible run-to-run.
- **`#[allow(...)]` or `# noqa` without a reason.** Every suppression needs a
  one-line justification, or it's not done.
- **Letting `ruff format` churn the diff mid-review.** Do substantive fixes first;
  run `ruff format src` as the final pass.
- **Forgetting the rebuild.** Any Rust edit ⇒ `devenv shell -- build` before
  pytest, or you test stale `.so`.

**Start by:** confirming HEAD is `569dbd5` and the tree is clean, then
re-measuring the baselines (`ruff check src --statistics`,
`cargo clippy --all-targets`) to confirm the inventory in §2 still holds, then
executing 14.1.
