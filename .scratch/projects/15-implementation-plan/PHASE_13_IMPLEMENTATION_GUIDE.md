# Phase 13 (V2) — Split the monolith files

> Execution guide for **Phase 13 of `REFINED_IMPLEMENTATION_PLAN_V2.md`**. This is
> the V1 "Phase 11" work, re-sequenced and extended to cover the new modules V2
> introduced (`bus/refinement.py`, `precision/refiner.py`).
>
> **Depends on Phases 1–12.** All behavioural change is done; this phase is
> **behaviour-preserving** restructuring. Run the full suite after **each** split.

---

## 0. Dev environment

All commands via `devenv shell --`. **Rust + Python.** Rebuild after any Rust move
that changes the extension. Suites are slow; run the full gate after each split.

---

## 1. What Phase 13 changes, and why

The three central files mix many responsibilities (V1 §6.3 deviation #10), which
hides ownership and lets side effects spread. The V2 refactor made the write
methods thin (`native op → validate → _after_commit → return`) and the graph a
pure projection — exactly the seams to cut along. **No behaviour changes.**

### Files in scope

| File | Split into |
|---|---|
| `rust/src/project.rs` | open / head-state / commit / snapshot / watch / authored / PyO3 wrappers (identity + code layer stay separate) (13.1) |
| `src/tyo3/session.py` | facade / views / post-commit hook / read wrappers / exceptions (13.2) |
| `src/tyo3/graph/graph.py` | applier / queries / diff / diagnostics / models — rename to reflect "projection" (13.3) |
| `src/tyo3/bus/`, `src/tyo3/precision/` | confirm clean module boundaries for the V2 additions (13.4) |

---

## 2. Working rules

1. **Behaviour-preserving.** The full suite is green after each split; public
   imports (`from tyo3 import TyO3Session`, etc.) still work.
2. **Thin PyO3 wrappers / thin facade.** Wrappers parse args, call core, convert
   errors. `TyO3Session` stays a thin facade.
3. **Break import cycles** by moving protocols/models out of implementation
   modules.
4. **One responsibility per module.**

---

## 3. Step-by-step

### Step 13.1 — Split `rust/src/project.rs`

Into focused modules: `open`, `head_state`, `commit` (the Phase-5 staged
transaction + the Phase-6 producer driver), `snapshot`, `watch`, `authored`, and
the PyO3 method wrappers. Identity (`identity.rs`) and the code layer
(`code_layer.rs`) stay in their own modules. Rebuild + full Rust gate after.

**Verify.** `devenv shell -- build && devenv shell -- cargo test --manifest-path rust/Cargo.toml`

### Step 13.2 — Split `src/tyo3/session.py`

Into facade / snapshot+latest views / the `_after_commit` post-commit hook /
shared read wrappers / public exceptions. Move protocols and models out of
implementation modules to avoid cycles. The public `TyO3Session` stays a thin
facade re-exporting the same surface.

**Verify.** `devenv shell -- pytest -q --no-cov`

### Step 13.3 — Split `src/tyo3/graph/graph.py`

Into applier / queries / diff / diagnostics / export / models. Since the graph is
now a *projection* (not a transaction participant), name it accordingly (e.g. a
`projection`/`snapshot_graph` name) so its role is unambiguous.

**Verify.** `devenv shell -- pytest src/tyo3/graph/tests/ -q --no-cov`

### Step 13.4 — Confirm the V2 additions' boundaries

`bus/` (bus / subscription / delta / interest / refinement) and `precision/`
(refiner) each have one clear responsibility; no cross-imports that recreate a
cycle.

> Commit here: `refactor: split project / session / graph monoliths`.

---

## 4. Acceptance

```bash
devenv shell -- build
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria:** full suite green after each split; public imports unchanged;
each module has a single clear responsibility.

## 5. Pitfalls

- **A behaviour change sneaking into a "pure move."** Diff carefully; the suite is
  the guard. Split one file at a time, gate between.
- **Import cycles** from leaving protocols/models in implementation modules.

## 6. Leaves for later

- Warnings/typing/hygiene + the end-to-end acceptance test — Phase 14.
