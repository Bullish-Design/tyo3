# Phase 7 (V2) — Pure-projection bus; delete the read-surface scaffolding

> Execution guide for **Phase 7 of `REFINED_IMPLEMENTATION_PLAN_V2.md`**. Read
> Concept V2 §4.2 / §5 and §6 (what V2 deletes) first.
>
> **This phase supersedes the V1 "Phase 7 = repair derived layers"** (that work
> moved to **V2 Phase 8**) **and completes the V1 "Phase 6 = bus."** The interim
> commit `aceabf6` landed an Option-B transitive bridge in the bus delta
> (`_compute_affected`/`_resolve_files` over the materialised head graph) as a
> stopgap while the producer was deferred. **Phase 6 removed the reason for that
> bridge** — `affected_ids` is now transitive at the source — so Phase 7 deletes
> the bridge, deletes the legacy read-surface graph builder, demotes the parity
> oracle, finishes the one-post-commit-path bus contract, and adds the
> refinement-channel seam.
>
> **Depends on Phase 6.** If `affected_ids` is not yet transitive at the source,
> stop and finish Phase 6 — deleting the bridge before then re-opens the
> reverse-dep delivery gap (`test_gate8_bus::test_scoped_reverse_dep_delivery`).

---

## 0. Dev environment

All commands via `devenv shell --`. This phase is **pure Python** except for
deleting now-dead native read-surface entry points; run `devenv shell -- build`
once before the final gate. Suites are slow (~10–15 min).

---

## 1. What Phase 7 changes, and why

After Phase 6 the bus consumes a `CommitDelta` whose `affected_ids` is already
the transitive, container-granular closure. So:

- **The bus delta becomes a pure projection.** `Delta.from_commit_delta` reads
  `affected` straight from the delta — the id-keyed transitive walk and the
  materialised-head-graph dependency are dead weight (Concept V2 §6).
- **The legacy read-surface builder is dead.** `CodeGraph` has been a pure
  applier since Phase 4; the ~1500-line read-surface build path in
  `graph/graph.py` has no caller once parity is retired. Delete it.
- **The parity oracle becomes a focused native-correctness suite.** With the
  legacy build gone there is nothing to compare against; keep native-correctness
  scenarios + the inference-flow regression tests.
- **The one post-commit path is finished.** `_after_commit(delta)`,
  publish-from-every-write (incl. `discard`/`author`), non-blocking overflow
  policies, revision-order **assertion** — the clean completion of the old Phase 6
  (reconcile whatever `aceabf6` landed).
- **The refinement-channel seam is added.** A revision-stamped affected-refinement
  for R may arrive after R's primary delta — the contract Phase 9 emits on.

### Files in scope

| File | Role |
|---|---|
| `src/tyo3/bus/delta.py` | **edit (core)** — `from_commit_delta` reads `affected` directly; delete `_compute_affected`, `_resolve_files`, the `graph`/`root` params, head-graph dependency (7.1) |
| `src/tyo3/graph/graph.py` | **edit (core)** — delete the read-surface builder; keep applier + algorithms (7.2) |
| `src/tyo3/graph/tests/test_incremental_parity.py` | **edit** — drop the legacy comparator; keep native-correctness scenarios (7.3) |
| `src/tyo3/session.py` | **edit** — finish `_after_commit`; publish from every write; drop the head-graph arg to `_publish_delta` (7.4) |
| `src/tyo3/bus/bus.py` | **edit** — revision-order assertion; refinement-channel delivery (7.4, 7.5) |
| `src/tyo3/bus/subscription.py` | **edit** — non-blocking policies only (carry from old Phase 6 if not done) (7.4) |
| `src/tyo3/bus/refinement.py` (new) | **add** — `AffectedRefinement` + delivery contract (7.5) |
| `src/tyo3/tests/test_final_bus_contract.py` | all xfail removed; refinement-channel contract test (7.4, 7.5) |

---

## 2. Working rules

1. **No graph in the bus.** After 7.1, `grep -n "CodeGraph\|_compute_affected\|_resolve_files" src/tyo3/bus/` returns nothing.
2. **No read-surface builder anywhere.** After 7.2, `graph/graph.py` contains only
   applier + algorithm code.
3. **Don't weaken the bus contract.** `test_final_bus_contract.py` ends fully green
   (zero xfail).
4. **The refinement channel is contract-only this phase.** Add the type + ordered
   delivery; **nothing emits a refinement yet** (Phase 9). Prove with a manual-inject test.
5. **Behaviour-preserving on the success path.** `test_gate8_bus` stays green.

---

## 3. Step-by-step

### Step 7.1 — Bus delta becomes a pure projection

```python
@classmethod
def from_commit_delta(cls, delta: CommitDelta) -> Delta:
    return cls(
        revision=delta.revision,
        created=frozenset(delta.created_ids),
        changed=frozenset(delta.changed_ids),
        deleted=frozenset(delta.deleted_ids),
        moved=frozenset(m.id for m in delta.moved),
        authored=frozenset(delta.authored_ids),
        affected=frozenset(delta.affected_ids),    # already transitive (Phase 6)
        rescan=delta.rescan,
        files=frozenset(delta.touched_files),       # native emits project-relative
        layers=_layers_touched(delta),
    )
```
Delete `_compute_affected`, `_resolve_files`, the `graph`/`root` parameters, and
any `_to_relative` path math (have the native commit emit project-relative
`touched_files`). Update `_publish_delta` (`session.py`) to drop the
`self._head_graph` / `root=` arguments.

**Verify.**
```bash
devenv shell -- pytest src/tyo3/tests/test_final_bus_contract.py::test_bus_deltas_are_id_level_and_in_revision_order -q --no-cov -rA
devenv shell -- pytest src/tyo3/tests/test_gate8_bus.py -q --no-cov
grep -n "CodeGraph\|_compute_affected\|_resolve_files\|_head_graph" src/tyo3/bus/delta.py   # expect nothing
```

### Step 7.2 — Delete the legacy read-surface builder

Identify `CodeGraph.build` and its read-surface helpers / identity priming; delete
them and orphaned helpers. Any graph construction now applies a `code_delta`
(snapshots already do this since Phase 4). Confirm no caller remains, then delete.

**Verify.**
```bash
devenv shell -- pytest src/tyo3/tests/test_graph_apply_code_delta.py src/tyo3/tests/test_gate8_bus.py -q --no-cov
```

### Step 7.3 — Demote the parity oracle

Keep the *scenarios* as native-correctness tests (apply a known delta → assert the
expected graph) + the inference-flow regression tests; delete the
"legacy-build vs delta-build" comparator.

**Verify.** `devenv shell -- pytest src/tyo3/graph/tests/ -q --no-cov`

### Step 7.4 — Finish the one post-commit path + bus contract

Complete (or confirm, if `aceabf6` did parts): single `_after_commit(delta)`;
publish from `edit`/`edit_many`/`edit_virtual`/`sync_path`/`discard`/`sync_all`/
`author`/`poll_changes`; non-blocking overflow policies
(`coalesce`/`drop_and_mark_lagged`/`error_and_close`, no producer-side
`_cond.wait`); promote the revision-order check in `Bus.publish` from a log to
`assert delta.revision > last`.

**Verify.**
```bash
devenv shell -- pytest src/tyo3/tests/test_final_bus_contract.py -q --no-cov -rA
grep -n "_cond.wait" src/tyo3/bus/subscription.py   # only consumer-side waits
```

### Step 7.5 — Add the refinement-channel seam

New `bus/refinement.py`:
```python
@dataclass(frozen=True)
class AffectedRefinement:
    revision: int               # the revision being refined
    narrowed: frozenset[str]    # precise affected (subset of the coarse set)
    # expansion mode may instead carry `added: frozenset[str]`
```
`Bus.publish_refinement(ref)` delivers to subscribers whose interest intersects
the refinement, on a channel **separate** from the primary delta stream (so the
primary `assert revision > last` invariant is untouched). Subscribers reconcile a
refinement against the primary delta they already saw for that revision.

This phase emits **none** — add a test that manually publishes a refinement for an
already-delivered revision and asserts ordered delivery.

> Commit here: `refactor(bus): pure-projection delta; delete read-surface builder + bridge; refinement channel`.

---

## 4. Acceptance

```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_bus_contract.py src/tyo3/tests/test_gate8_bus.py src/tyo3/graph/tests/ -q --no-cov -rA
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria:** bus delta is a pure projection (no graph, no bridge); legacy
read-surface builder deleted; parity oracle demoted; one post-commit path with
every write publishing; revision order asserted; non-blocking overflow only;
refinement channel contract in place (emitting nothing yet); both suites green.

---

## 5. Pitfalls

- **Deleting the bridge before Phase 6.** Re-opens the reverse-dep delivery gap.
- **Leaving a `graph=`/`root=` parameter "just in case."** Invites a caller to
  reintroduce path→id reconstruction. Delete them.
- **Mixing the refinement channel into the primary revision-order assertion.**
  Keep them separate or the assertion trips on a late refinement.
- **A surviving producer-side `_cond.wait`.** Re-creates the writer stall. Grep.

---

## 6. What Phase 7 leaves for later

- **Emitting refinements** — Phase 9.
- **Derived invalidation consuming `affected`** — Phase 8.
- **Bus file/module structure** — Phase 13.
