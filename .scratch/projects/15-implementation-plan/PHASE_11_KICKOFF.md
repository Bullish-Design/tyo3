We're continuing the TyO3 spine refactor. All planning is done and lives in
`.scratch/projects/15-implementation-plan/`. Your job this session is to implement
**Phase 11: Read surface & convenience APIs** — make every convenience read either
**own/pin the snapshot it reads over** or go through an explicit pinned snapshot,
so no caller is ever handed a **view backed by an already-closed snapshot**; keep
the floating "latest" surface honestly single-layer; and stop the layer views
**swallowing read errors into `None`** (typed absence vs typed failure).

This is **pure Python** (`src/tyo3/session.py`, `src/tyo3/layers/*.py`). **No Rust,
no rebuild in the inner loop** — just run the gates. (If you somehow touch Rust,
`devenv shell -- build` before any pytest gate, but you shouldn't need to.)

Before writing any code, read in this order:
  1. `.scratch/projects/15-implementation-plan/START_HERE_V2.md`         (orientation — read fully; note the V2 phase-numbering table)
  2. `REFINED_IMPLEMENTATION_CONCEPT.md` (V1) §5.2 and §5.12             (snapshot isolation/lifetime + the error model — normative)
  3. `REFINED_IMPLEMENTATION_PLAN_V2.md`  (Phase 11 section)             (current state + phase map)
  4. `PHASE_11_IMPLEMENTATION_GUIDE.md`                                  (the step-by-step 11.1–11.3 you'll execute)
Then skim, for current ground truth:
  5. `.scratch/projects/16-refined-implementation-work/PROGRESS.md` §9.9 and §9.10  (what Phases 9 and 10 landed)

## Critical context — Phases 6–10 are LANDED

- **Phase 10 just landed (commit `521fe5b`)**: content hashing is now AST-canonical,
  the container-subsumes-members invariant is documented + tested, and the full
  suite + `cargo test` (167/0) are clean. The lone historical baseline failure
  (`test_final_hash_ast::test_formatting_only_hashes_same`) is now green and the two
  `xfail(strict=True)` tests are unmarked and passing. **The baseline going into
  Phase 11 is a fully green `pytest -q --no-cov`.** Don't reintroduce a failure.
- **Phase 9 (commit `74adf25`)** added the async precision refiner. Its snapshots
  are opened with `session.snapshot(at=R)` and **already close in a `finally`**
  (`src/tyo3/precision/refiner.py:229-232`). Phase 11's guide lists "review the
  refiner's snapshot lifetime" as a carry-over — **verify it's still correct, don't
  assume you must change it.** If it's already `finally`-closed (it is), just note
  that in the PROGRESS record.
- Phases 6–8 (producer / pure-projection bus / unified derived invalidation) are the
  spine underneath the read surface. **Reads must never advance the head revision**
  (V1 §5.9 / deviation #3) — that property is guarded by
  `test_final_no_read_side_writes.py` and must stay green.

## The concrete bug — what exists vs what you build

**The exact defect (V1 §6.3 deviation #7) is live at `src/tyo3/session.py:1481-1506`.**
The convenience reads open a fresh snapshot, take a lazy view, **close the snapshot,
then return the view**:

```python
@property
def code(self):
    snap = self.snapshot()
    code_view = snap.code
    snap.close()        # ← snapshot closed …
    return code_view    # ← … but the returned view reads over it. Later access fails.

def layer(self, name: str):
    snap = self.snapshot(); view = snap.layer(name); snap.close(); return view

def entity(self, durable_id: str):
    snap = self.snapshot(); ev = snap.entity(durable_id); snap.close(); return ev

def diff(self, before):
    after_snap = self.snapshot(); result = after_snap.diff(before); after_snap.close(); return result
```

(`diff` may be safe if it eagerly materialises before close — **verify** whether its
result is eager or lazy before deciding it needs the same fix.)

**You build:** one consistent lifetime shape for these (see 11.1), applied to
`code` / `layer` / `entity` / `diff` (and any sibling at `session.py:~1604-1669`,
the latest/warm surface — there appear to be **two** read-surface classes; reconcile
both). **You do not** rewrite the Rust snapshot machinery or the layer-view value
semantics — only *who owns the snapshot* and *how absence/failure is reported*.

## The decision you must make and state (11.1) — pick ONE shape, apply consistently

The guide offers two; choose deliberately and justify:
  - **(preferred) Remove the lazy convenience sugar; reads go through an explicit
    pinned snapshot** — `with session.snapshot() as snap: snap.code.value(id)`. Callers
    own the lifetime; the footgun is gone by construction. Downside: touches call
    sites/tests that used `session.code` as a one-liner.
  - **(alternative) The returned view owns its snapshot and is itself a context
    manager** that closes on `__exit__`. Keeps the one-liner ergonomics but the
    "owns its snapshot" variant has its own footgun — **don't close in the wrong
    scope** (the Pitfall the guide calls out: you reintroduce the exact bug if the
    factory closes before handing back).

**Check the target tests first** (`test_gate7_read_surface.py` — see the test list
below) to see which shape they already expect; let the tests drive the choice rather
than inventing an API they don't use. State your choice and why in the PROGRESS record.

## What Phase 11 changes — the three steps

- **11.1 — Remove closed-snapshot views.** Fix `session.py:1481-1506` (+ the second
  read-surface class) per your chosen shape. **No view over a closed snapshot, ever.**
  *Verify:* `devenv shell -- pytest src/tyo3/tests/test_gate7_read_surface.py -q --no-cov -rA`
- **11.2 — Keep the floating "latest" view honest.** The floating-latest surface is
  for **warm single-layer reads only**: no entity accessor, no cross-layer diff, no
  pinned revision, no mutable graph reference. If it keeps a `graph()` accessor it must
  return a clearly **non-canonical** projection (named/typed as such, not something
  that looks like the canonical head graph). There are `graph()` accessors at
  `session.py:741`, `:1669`, `:1906` — **identify which belongs to the floating-latest
  surface and mark it non-canonical.**
- **11.3 — Stop swallowing read-surface errors.** The layer views catch broad
  `except Exception: return None` and conflate "absent" with "backend/format/graph-build
  failure" (V1 §5.12). Live sites to fix (verify each before touching):
    - `src/tyo3/layers/authored.py:41,58,67-70,92`
    - `src/tyo3/layers/code.py:52,117`
    - `src/tyo3/layers/derived.py:55-56,77,99`
  **Distinguish typed absence (a real "not present" → `None`/empty is correct) from
  typed failure (backend error, graph-build failure, format/config error → raise a
  typed error).** Don't blanket-raise — some `return None` are legitimate absence;
  read each call site and classify it.
  *Verify:* `devenv shell -- pytest src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov`

## The target tests (this is the acceptance, be precise)

`src/tyo3/tests/test_gate7_read_surface.py` (15 tests) is the read-surface contract:
`test_cross_layer_join_entity_view`, `test_combined_snapshot_diff`,
`test_latest_warm_snapshot_consistent`, `test_entity_view_all_layers_describe_same_revision`,
`test_entity_view_absent_entity`, `test_code_diff_entity_body_changed`,
`test_code_diff_add_file_removed`, `test_derived_drift_on_body_edit`,
`test_authored_diff_changed`, `test_combined_diff_entities_union`,
`test_same_revision_diff_is_empty`, `test_code_diff_move_entity_unchanged`,
`test_latest_has_no_entity_no_diff`, `test_diff_parity_with_independent_rebuild`,
`test_code_only_project_no_op`.

`src/tyo3/tests/test_final_no_read_side_writes.py` is the no-read-side-writes guard.

**Run both, see which currently fail/pass, and let them define "done."** If they're
already green (the read surface may have been partially fixed), Phase 11 is about
making the *implementation* honest (lifetime ownership + typed errors) without
regressing them — confirm by reading the tests, not just running them.

## Things to verify before relying on them

- **Whether `session.diff` is actually buggy.** It follows the same pattern but may
  eagerly materialise its result before `close()`. Read `Snapshot.diff` — if eager,
  leave it; if it returns a lazy view, fix it like the others.
- **Two read-surface classes.** `session.py` has convenience reads at ~1481 *and*
  ~1604-1669 (looks like a head-pinned surface and a floating-latest surface).
  Map which is which before editing; 11.2's "floating latest" constraints apply to
  exactly one of them.
- **The refiner snapshot (Phase-9 carry-over).** `precision/refiner.py:229-232`
  already closes in `finally`. Confirm and move on.

## Working rules (every phase)

- Run EVERYTHING through `devenv shell --` (Nix toolchain). Never bare `pytest`.
- **Pure Python this phase — no rebuild needed** in the inner loop. (Rust rule still
  holds if you touch Rust: `devenv shell -- build` before any pytest gate.)
- Suites are slow (~10–15 min); run the full suite in the background.
- **Reads never advance the revision.** Confirm `test_final_no_read_side_writes` and
  the Phase-7 bus / Phase-8 derived / Phase-9 precision suites stay green.
- **Typed absence vs typed failure** — no broad `except: return None` left where the
  exception could be a real failure.

## The milestone gate after the phase

```bash
devenv shell -- pytest src/tyo3/tests/test_gate7_read_surface.py src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov -rA
devenv shell -- pytest -q --no-cov
```

**Exit criteria:** no convenience API returns closed lazy state; cross-layer reads
are pinned or explicitly floating; the floating-latest surface is single-layer +
non-canonical-graph; read paths surface typed errors (absence ≠ failure); no read
advances head; the full suite stays green (the Phase-10 baseline is fully green —
don't regress it). No new xfails/XPASS.

Track progress in `.scratch/projects/16-refined-implementation-work/PROGRESS.md`
(add a `§9.11 — V2 Phase 11` record alongside §9.9/§9.10). Commit at the end with
`fix(read): owned-lifetime convenience views; stop swallowing read errors`
(no AI attribution — house rule).

## Start here

Read the docs above (incl. PROGRESS §9.9/§9.10 and the actual current
`src/tyo3/session.py:1481-1506` + the ~1604-1669 surface, the `graph()` accessors at
`:741/:1669/:1906`, and the layer views in `src/tyo3/layers/{authored,code,derived}.py`
with their `except Exception: return None` sites), **read the two target test files to
see the API shape they expect**, then give me a short Phase 11 implementation plan:
steps 11.1–11.3 mapped to the real functions/lines you'll touch, your **lifetime-shape
decision** (remove-the-sugar vs view-owns-snapshot — with reasoning grounded in what
the gate tests actually call), which read-surface class is the floating-latest one and
how you'll mark its `graph()` non-canonical, and a per-site classification of the
`return None` cases into "legitimate absence (keep)" vs "swallowed failure (raise
typed)". Note which target test each step is gated by, and confirm the Phase-9 refiner
snapshot is already `finally`-closed.
