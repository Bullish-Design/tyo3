# Phase 14 (V2) — Zero warnings, typing, hygiene + end-to-end acceptance

> Execution guide for **Phase 14 of `REFINED_IMPLEMENTATION_PLAN_V2.md`**. This
> merges the V1 "Phase 12 (warnings/typing/hygiene)" and "Phase 13 (end-to-end
> acceptance)" into the final phase, extended to assert the V2 affected model and
> the optional precision refinement.
>
> **Depends on Phases 1–13.** This is the closing gate of the whole refactor.

---

## 0. Dev environment

All commands via `devenv shell --`. **Rust + Python.** Suites are slow; run the
full gate at the end.

---

## 1. What Phase 14 delivers

1. **Zero warnings + hygiene** (V1 §5.12): Rust warnings to zero with
   `clippy --all-targets -- -D warnings` in the gate; reduce untyped values;
   typed models for native payloads; factories for mutable defaults; replace broad
   `except Exception: pass` with typed handling.
2. **The end-to-end acceptance test** that proves the whole V2 story, including the
   transitive container-granular `affected` and (with `precision=method`) a
   refinement.

### Files in scope

| File | Role |
|---|---|
| `rust/src/*`, `src/tyo3/*` | warnings/typing/hygiene cleanup (14.1–14.3) |
| `src/tyo3/tests/test_final_acceptance.py` (new) | the end-to-end proof (14.4) |
| `devenv.nix` | add `clippy -D warnings` / ruff to the gate scripts if not present |

---

## 2. Working rules

1. **`-D warnings` clean, `ruff check` clean, `ruff format --check` clean.**
2. **Every deliberate swallow gets a justifying comment.**
3. **The acceptance test is deterministic** (fixed content/sidecar ⇒ reproducible
   ids/hashes/delta — V1 §5.12).

---

## 3. Step-by-step

### Step 14.1 — Zero Rust warnings

Remove unused imports, dead variants, confusing lifetime syntax; prefix
intentionally-unused test bindings with `_`; narrow `#[allow(...)]` only with a
one-line reason. Add `cargo clippy --manifest-path rust/Cargo.toml --all-targets
-- -D warnings` to the gate.

**Verify.** `devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`

### Step 14.2 — Typing

Reduce untyped values in the session, native type stubs, and stores/generators;
add typed models for native return payloads so wrappers validate less by hand.
Replace mutable default arguments/fields with factories.

### Step 14.3 — Exception hygiene

Replace every broad `except Exception: pass` with a typed catch that yields a
typed absence, logs with context, or re-raises a domain error.

**Verify.** `devenv shell -- ruff check src && devenv shell -- ruff format --check src`

### Step 14.4 — The end-to-end acceptance test

`src/tyo3/tests/test_final_acceptance.py` exercises one project end to end:
open → pin an initial snapshot → author intent on an entity → read the code graph
from a pinned snapshot → configure a deterministic derived layer (one **local**,
one **semantic**) → edit one entity → move another unchanged → delete one → read
old and new snapshots → diff them → verify bus notifications → (with
`precision=method`) verify a refinement narrows the affected set → close and reopen
→ verify identity + authored records survive → delete the derived cache and verify
recompute → assert no source files were modified.

Assertions:
- same revision ⇒ same content; durable ids survive a cosmetic edit and a move;
  content hash changes only on a meaningful edit;
- **`affected_ids` is the transitive container-granular closure** (a base-class
  edit reports its subclasses + their importers); never seeds-only;
- derived artifacts keyed by content hash; the **local** layer does not recompute
  on a dependency-only change; the **semantic** layer does;
- authored records present / needs-review / orphaned as appropriate;
- bus deltas ordered and id-level; with `precision=method`, a refinement for R
  narrows the affected set and arrives on the refinement channel;
- the snapshot diff agrees with an independent rebuild; no read accessor advanced
  head.

**Verify.** `devenv shell -- pytest src/tyo3/tests/test_final_acceptance.py -q --no-cov -rA`

> Commit here: `chore: zero warnings, typing, hygiene; end-to-end acceptance`.

---

## 4. Acceptance — the final gate (Definition of Done, V2)

```bash
devenv shell -- pytest -q                                   # no unexpected skips
devenv shell -- cargo test --manifest-path rust/Cargo.toml
devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
devenv shell -- ruff check src
devenv shell -- ruff format --check src
```

**Exit criteria (Concept V2 §9):**
- No snapshot construction reads live disk; no read advances head.
- Every write is one native commit → one id-level commit delta → one shared
  post-commit path; failed writes fully roll back; no torn publish.
- `affected_ids` is transitive at the source (container-granular, never-miss);
  proven natively, not by a Python walk.
- The bus delta is a pure projection; no read-surface builder; no Option-B bridge.
- Derived invalidation is one affected-driven loop with per-layer key locality.
- The async precision layer narrows correctly (`method`) and degrades gracefully;
  with `container` it does not run and the system is fully correct.
- Hashing is AST-canonical; the container-subsumes-members invariant is tested.
- All lints clean; the end-to-end acceptance test proves the whole story.

## 5. Pitfalls

- **A flaky acceptance test from non-determinism** (iteration order, timestamps).
  Pin everything; ids/hashes must be reproducible.
- **`#[allow(...)]` without a reason** — every one needs a one-line justification.
