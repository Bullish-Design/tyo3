# Phase 11 (V2) — Read surface and convenience APIs

> Execution guide for **Phase 11 of `REFINED_IMPLEMENTATION_PLAN_V2.md`**. Read
> V1 §5.2 (snapshot isolation / lifetime) and §5.12 (error model) once before
> starting. This is the V1 "Phase 8" work, unchanged in intent, re-sequenced.
>
> **Depends on Phases 1–10.** It also reviews the snapshot lifetimes opened by the
> Phase-9 precision refiner (those must close in `finally` too).

---

## 0. Dev environment

All commands via `devenv shell --`. **Pure Python** (`src/tyo3/session.py`, the
read views). No rebuild needed in the inner loop. Suites are slow.

---

## 1. What Phase 11 changes, and why

Convenience reads (`session.code`, `session.layer`, `session.entity`) currently
open a snapshot, take a lazy view, then **close the snapshot before returning** —
so later access fails (V1 §6.3 deviation #7). And layer views catch broad
exceptions and return `None`, hiding failures.

### Files in scope

| File | Role |
|---|---|
| `src/tyo3/session.py` | **edit (core)** — convenience reads own or pin their snapshot lifetime; floating-latest honesty (11.1, 11.2) |
| `src/tyo3/layers/`, view modules | **edit** — stop swallowing read errors; typed absence vs failure (11.3) |
| `src/tyo3/precision/refiner.py` | **review** — its snapshots close in `finally` (Phase 9 carry-over) |
| `src/tyo3/tests/test_gate7_read_surface.py`, `test_final_no_read_side_writes.py` | the gate (11.1–11.3) |

---

## 2. Working rules

1. **No view over a closed snapshot, ever.**
2. **Reads never advance the revision** (V1 §5.9 / deviation #3) — confirm
   `test_final_no_read_side_writes` stays green.
3. **Typed absence vs typed failure** — no broad `except: return None`.

---

## 3. Step-by-step

### Step 11.1 — Remove closed-snapshot views

Pick one shape and apply consistently:
- *Preferred:* remove the lazy convenience reads; reads go through an explicit
  pinned snapshot — `with session.snapshot() as snap: snap.code.value(id)`.
- *Alternative:* the returned view owns its snapshot and is itself a context
  manager that closes it on exit.

Never return a lazy view tied to an already-closed snapshot.

**Verify.** `devenv shell -- pytest src/tyo3/tests/test_gate7_read_surface.py -q --no-cov -rA`

### Step 11.2 — Keep the floating "latest" view honest

The floating latest read surface is for warm single-layer reads only: no entity
accessor, no cross-layer diff, no pinned revision, no mutable graph reference. If
it keeps a `graph()` accessor, it returns a clearly non-canonical projection.

### Step 11.3 — Stop swallowing read-surface errors

Layer views stop catching broad exceptions and returning `None`. Distinguish
typed absence from typed backend failure, graph-build failure, and format/config
failure (V1 §5.12).

**Verify.** `devenv shell -- pytest src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov`

> Commit here: `fix(read): owned-lifetime convenience views; stop swallowing read errors`.

---

## 4. Acceptance

```bash
devenv shell -- pytest src/tyo3/tests/test_gate7_read_surface.py src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov -rA
devenv shell -- pytest -q --no-cov
```

**Exit criteria:** no convenience API returns closed lazy state; cross-layer reads
are pinned or explicitly floating; read paths surface typed errors; no read
advances head.

## 5. Pitfalls

- **Returning a view whose snapshot the function already closed.** The exact bug
  this phase fixes — don't reintroduce it in the "owns its snapshot" variant by
  closing in the wrong scope.
- **A floating-latest `graph()` that looks canonical.** Mark it non-canonical.

## 6. Leaves for later

- Module structure — Phase 13.
