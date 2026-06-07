# Phase 6 — One Python post-commit path; non-blocking bus

> A step-by-step execution guide for **Phase 6** of
> `REFINED_IMPLEMENTATION_PLAN.md`. Read that plan's "Phase 6" section and §5.11
> (the subscription bus) / §5.3 (publication is the last in-lock step) / §5.12
> (error model) of `REFINED_IMPLEMENTATION_CONCEPT.md` once before starting —
> this guide assumes that vocabulary (the id-level `CommitDelta`, the single
> write transaction, scoped/ordered/non-blocking delivery, the per-subscriber
> bounded queue and its overflow policy) and turns it into concrete edits
> against the code as it exists today.
>
> **Phase 6 depends on Phases 1–5.** Phase 3 reshaped the public write result
> into the id-level `CommitDelta` (`created_ids / changed_ids / deleted_ids /
> moved[{id, old, new}] / authored_ids / affected_ids / code_delta /
> touched_files / rescan`) and mirrored it as a Python model. Phase 4 made the
> native code delta authoritative and turned `CodeGraph` into a **pure applier**
> (`apply_code_delta`) with no read-side writes. Phase 5 funnelled every write
> kind through one native `commit(mutation)` that stages all state and
> **publishes the revision last**, returning that one `CommitDelta`. Phase 6 is
> the *Python-side* counterpart: it collapses the per-method post-commit
> sequences into **one** `_after_commit(delta)` hook so no write path can
> diverge, makes **every** write path publish (today `discard` does not), turns
> the bus delta into a thin id-level wrapper over the commit delta, and
> **removes the writer-blocking overflow policy** so a slow subscriber can never
> stall the writer. If any earlier phase's milestone gate is not green, stop and
> finish it first.

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
  devenv shell -- pytest src/tyo3/tests/test_final_bus_contract.py -q --no-cov
  ```
- Interactive (if you are iterating rapidly):
  ```bash
  devenv shell        # drop into the environment once
  # …then run pytest / cargo / the project scripts directly inside it…
  ```

The project defines custom scripts inside `devenv.nix` — `build`, `tests`,
`test-rust`, `test-quick`, `clean`, `status`, etc. When this guide says "rebuild
the extension," it means `devenv shell -- build` (which runs maturin so Python
sees the freshly compiled Rust). **A pure `cargo test` does not refresh the
compiled extension Python imports.**

Phase 6 is *mostly* Python — the post-commit hook, the bus, the delta wrapper —
but it has **one native touch**: config validation must reject a writer-blocking
overflow policy (6.3), which lives in `rust/src/config.rs`. After **that** Rust
change, you must `devenv shell -- build` before the pytest gate observes it.
Forgetting the rebuild is the single most common way to chase a phantom failure
(`test_config_rejects_writer_blocking_overflow_policy` would keep passing/failing
against the *old* compiled extension). For the pure-Python edits a rebuild is not
required, but running `devenv shell -- build` once before the final gate costs
nothing and removes all doubt.

The suites are slow. Allow **~10–15 minutes** for a full run; launch full suites
in the background and give them time rather than assuming a hang.

> Throughout the rest of this document, **assume every `pytest`, `cargo`,
> `maturin`, `ruff`, and project-script invocation is prefixed with
> `devenv shell --`**, even where a line is abbreviated for readability.

---

## 1. What Phase 6 changes, and why

**Goal (from the plan):** every write runs the same post-commit steps, so none
can diverge, and the bus never blocks the writer (§5.11).

**The situation today** (confirmed in the current code):

- **Every write method hand-copies its post-commit sequence.** `edit`
  (`session.py:939`), `edit_many` (`:955`), `edit_virtual` (`:970`), `sync_path`
  (`:986`), `discard` (`:1002`), `sync_all` (`:1016`), `poll_changes` (`:1069`)
  and `author` (`:1223`) each repeat some subset of:
  `self._invalidate_head_snap()` → native op → `SyncResult.model_validate(...)` →
  `self._apply_graph_delta(result)` → `self._publish_delta(result)`. Because the
  sequence is copied by hand, the copies have drifted — which is exactly the
  defect §6.3 of the concept names.

- **`discard` commits a revision but never publishes.** `discard`
  (`session.py:1002-1014`) calls `_apply_graph_delta(result)` and then **returns
  without `_publish_delta(result)`** — a committed revision no subscriber ever
  hears about (§5.11 violation, and the headline of
  `test_final_bus_contract.py::test_every_write_kind_publishes_exactly_one_delta`,
  currently `xfail(strict=True)`).

- **`author` publishes but does not apply a graph delta** (correct — authored
  writes touch no code/derived structure) yet still re-implements its own tail
  (`session.py:1240-1241`). One more bespoke sequence to fold in.

- **The bus delta is reconstructed from path-shaped data via a graph walk.**
  `Delta.from_sync_result` (`bus/delta.py:109`) takes the native result's
  **absolute file paths** (`result.created/changed/deleted/moved`) and maps them
  back to `DurableId`s through `CodeGraph._file_to_nodes` / `_id_to_index`
  (`_ids_in_files` `:206`, `_compute_affected` `:245`, `_resolve_files` `:268`).
  When the graph is not materialised it falls back to "treat the file path as a
  durable id" (`bus/delta.py:154-159`). So bus ids are path-shaped unless the
  graph happens to be built — the second `xfail` contract
  (`test_bus_deltas_are_id_level_and_in_revision_order`). After Phase 3 the
  commit delta is **already** id-level; rebuilding ids from paths is now both
  redundant and lossy.

- **The bus carries a writer-blocking overflow policy.** The `Bus`
  (`bus/bus.py:31-36`) and `Subscription` (`bus/subscription.py:42-90`) accept
  `overflow ∈ {"coalesce", "block", "error"}`. The `"block"` branch in
  `_offer` (`subscription.py:81-87`) calls `self._cond.wait()` **on the producer
  side** — and the producer is the writer. A slow/dead subscriber on a full queue
  stalls the commit. Config does not reject it (`config.rs` `validate` `:294` has
  no overflow check; `default_overflow` `:583` only sets the default). This is
  the §5.11 "a slow or dead subscriber MUST NOT stall the writer" violation and
  `test_config_rejects_writer_blocking_overflow_policy` (xfail).

- **The bus's revision-order check is a warning, not a guarantee.**
  `Bus.publish` (`bus/bus.py:92-101`) *logs an error* if a delta arrives out of
  revision order but proceeds anyway — a guarantee the code documents but does
  not hold. After Phase 5, publication is the strictly-last in-lock step and the
  native commit serialises all writers, so revision order is now a real
  invariant the bus can **assert**.

**What Phases 1–5 already put in place (you build on it, don't rebuild it):**

- One native `commit(mutation)` returns one id-level `CommitDelta` for every
  write kind, published last (Phase 5). The Python model mirrors it with
  `changed_ids` / `affected_ids` / `created_ids` / `deleted_ids` / `moved` /
  `authored_ids` / `touched_files` / `rescan` / `code_delta` (Phase 3.4).
- `CodeGraph.apply_code_delta(delta.code_delta)` is the pure applier (Phase 4),
  callable from the post-commit hook with no FFI / no read-surface walk.
- Derived invalidation already accepts the id-level dirty/deleted sets — Phase 7
  *tightens* it, but Phase 6 only needs to **call** it from the one hook.

**The fix this phase delivers:**

1. **One post-commit hook** `_after_commit(delta)` (6.1) that every write method
   calls; the per-method tails collapse into it.
2. **Publish from every write path** (6.2), including `discard` and `author`;
   promote the bus revision-order check from a warning to an assertion.
3. **Remove the writer-blocking overflow policy** (6.3): config rejects any
   blocking policy; the supported policies are all non-blocking — `coalesce`,
   `drop_and_mark_lagged`, `error_and_close`.
4. **Scoped, ordered, id-level deltas** (6.4): the bus delta is a thin immutable
   wrapper over the commit delta, built directly from its ids — no graph walk.
   Interest matching is id-level (an id interest matches `affected_ids`; a file
   interest matches `touched_files` plus the files of affected ids; a layer
   interest matches touched layers; `rescan` matches all).

### Why one post-commit path is the whole game

§6.3 of the concept is explicit: *one write path forgets to publish* because each
method hand-copies the sequence. The structural fix is to have exactly **one**
place that runs the post-commit steps, so "publish every revision" is true by
construction rather than by remembering to copy a line. After Phase 6 a write
method is: `delta = native_op(...)` → `validate(delta)` → `_after_commit(delta)`
→ `return delta`. There is no second sequence to drift from.

### Files in scope

| File | Role in Phase 6 |
|---|---|
| `src/tyo3/session.py` | **edit (core)** — add `_after_commit(delta)`; route `edit`/`edit_many`/`edit_virtual`/`sync_path`/`discard`/`sync_all`/`poll_changes`/`author` through it; make `discard` and `author` publish (6.1, 6.2) |
| `src/tyo3/bus/delta.py` | **edit (core)** — make `Delta` a thin wrapper over the id-level `CommitDelta`; replace `from_sync_result` + the graph-walk helpers with `from_commit_delta` (6.4) |
| `src/tyo3/bus/bus.py` | **edit** — drop the `"block"` overflow type; promote the revision-order check to an assertion; deliver every matched revision (incl. ALL) (6.2, 6.3, 6.4) |
| `src/tyo3/bus/subscription.py` | **edit** — rename overflow policies to `coalesce` / `drop_and_mark_lagged` / `error_and_close`; delete the blocking `_offer` branch (6.3) |
| `src/tyo3/bus/interest.py` | reference / small edit — id/file/layer matching already exists; confirm file-interest matches `touched_files` + affected-id files (6.4) |
| `rust/src/config.rs` | **edit** — `validate` rejects a writer-blocking overflow policy with `ConfigError` (6.3) |
| `src/tyo3/tests/test_final_bus_contract.py` | the Phase 0 test that must go green this phase (do **not** weaken it) — remove its three `xfail` markers as each contract lands |
| `src/tyo3/tests/test_gate8_bus.py` | the regression surface that must stay green over the restructured bus |

---

## 2. Working rules for this phase (non-negotiable)

1. **Write/await the failing test first, then implement until green.** Phase 0
   shipped `test_final_bus_contract.py` with three `xfail(strict=True)` cases
   (`:64`, `:107`, `:157`) and one already-passing case
   (`test_slow_subscriber_does_not_block_writer`, `:133`). Your job removes the
   reason for each xfail; remove a marker **only** once that case passes on its
   own (an xfail that starts passing with the marker still on is itself a failure
   under `xfail-strict`).
2. **Exactly one post-commit path.** After 6.1 there must be **one**
   `_after_commit` and **zero** other call sites of `_apply_graph_delta` /
   `_publish_delta` / `_invalidate_head_snap` in the write methods. Grep for them
   and confirm the only callers are inside `_after_commit` (plus the legitimate
   non-write callers: `reload`, `close`, snapshot invalidation).
3. **Every committed write publishes.** `edit`, `edit_many`, `edit_virtual`,
   `sync_path`, `discard`, `sync_all`, `author`, and `poll_changes` each publish
   exactly one delta when they commit a revision. A `poll_changes` that commits
   nothing (no events) returns `None` and publishes nothing — that is correct,
   not a missed publish.
4. **The bus never blocks the writer.** There must be **no** code path on the
   producer side (`Subscription._offer`, called by `Bus.publish`) that waits on a
   condition variable. The blocking `"block"` branch is deleted, not guarded.
   `test_slow_subscriber_does_not_block_writer` must stay green and fast.
5. **The bus delta is id-level and a thin wrapper.** Build `Delta` from the
   commit delta's id fields directly; do not reconstruct ids from file paths via
   the graph. The graph-walk helpers (`_ids_in_files`, `_compute_affected`,
   `_resolve_files`) are deleted once `affected_ids` comes from the commit delta.
6. **Don't reshape the commit delta or the native commit.** Phase 6 consumes the
   Phase 3 `CommitDelta` and the Phase 5 commit; it does not change either. The
   bus `Delta` is a *projection* of the commit delta, not a new source of truth.
7. **Fail loudly on bad coordination config.** Rejecting the blocking overflow
   policy is a typed `ConfigError` at open, never a silent fallback to a default.
   (Full config unification is Phase 10; Phase 6 adds only this one validation.)
8. **Milestone gate after the phase** (both must pass, both via devenv):
   ```bash
   devenv shell -- pytest -q --no-cov
   devenv shell -- cargo test --manifest-path rust/Cargo.toml
   ```

---

## 3. Pre-flight — establish the red baseline

Confirm the starting state before changing anything, so you can prove your work
moved the needle.

```bash
# 1. Phases 1–5 are landed: the native commit + id-level delta are in place and
#    the Rust core is green.
devenv shell -- build
devenv shell -- cargo test --manifest-path rust/Cargo.toml \
  project commit_delta code_delta identity authored sidecar config

# 2. The bus contract test is RED in exactly the expected way: three cases are
#    xfail(strict=True) (discard never publishes; bus ids are path-shaped; the
#    blocking overflow policy is accepted) and one case already passes.
devenv shell -- pytest src/tyo3/tests/test_final_bus_contract.py -q --no-cov -rA

# 3. The existing bus suite is green — your regression target.
devenv shell -- pytest src/tyo3/tests/test_gate8_bus.py -q --no-cov
```

Record the output. Two North Stars: (a) the three xfail cases in
`test_final_bus_contract.py` become real green assertions and their markers are
removed by the end of Phase 6; (b) `test_gate8_bus` stays green throughout — the
bus restructuring must be behaviour-preserving on the success path.

> **A note on the post-Phase-5 method bodies.** This guide refers to the write
> methods by their *current* (pre-Phase-6) line numbers (`session.py:939` etc.).
> After Phases 3–5 those bodies validate a `CommitDelta` (not the old
> `SyncResult`) and return it. Read the actual code first; where this guide says
> "the validated delta," it means whatever the post-Phase-5 native op returns and
> the Python model mirrors (`CommitDelta` with `changed_ids` / `affected_ids` /
> …). If a name differs in your tree, match the tree, not this prose.

---

## 4. Step-by-step implementation

Do the steps in order. Each step lists **what**, **why**, **where**, and a
**verify** command (devenv-prefixed). Commit at the natural breakpoints noted.

> **Sequencing matters and is deliberate.** Land the id-level delta wrapper
> (6.4) first so the bus has something correct to carry; then centralise the
> post-commit hook (6.1) so every path uses it; then make every path publish
> (6.2); then remove the blocking overflow policy and add the config validation
> (6.3). Removing each xfail marker is the last move of the step that earns it.

### Step 6.4 (do first) — Scoped, ordered, id-level deltas

**What.** Make the bus `Delta` a thin immutable wrapper over the id-level
`CommitDelta`: build it directly from the delta's id fields, delete the
file-path-to-id graph walk, and confirm interest matching is id-level.

**Why.** §5.11 / Phase 6.4. After Phase 3 the commit delta already carries
`changed_ids`, `affected_ids`, etc. as durable ids; reconstructing ids from file
paths via the graph (`bus/delta.py:206-292`) is redundant, lossy (it depends on
the graph being materialised), and the reason
`test_bus_deltas_are_id_level_and_in_revision_order` is xfail. Building the
`Delta` from the commit delta makes ids correct unconditionally.

**Where / how.**
- In `bus/delta.py`, add a constructor that takes the `CommitDelta` directly:
  ```python
  @classmethod
  def from_commit_delta(cls, delta: CommitDelta) -> Delta:
      """Wrap an id-level CommitDelta for bus delivery (§5.11).

      Pure projection: every id set comes straight from the commit
      delta — no graph walk, no path→id reconstruction.
      """
      created = frozenset(delta.created_ids)
      changed = frozenset(delta.changed_ids)
      deleted = frozenset(delta.deleted_ids)
      moved = frozenset(m.id for m in delta.moved)
      authored = frozenset(delta.authored_ids)
      affected = frozenset(delta.affected_ids)
      files = frozenset(delta.touched_files)
      layers = _layers_touched(delta)   # "code" on any structural change; authored layer(s) on authored writes
      return cls(
          revision=delta.revision,
          created=created, changed=changed, deleted=deleted, moved=moved,
          authored=authored, affected=affected,
          rescan=delta.rescan, files=files, layers=layers,
      )
  ```
  `affected_ids` is computed natively in the commit (Phase 3.3) as the closure of
  `changed ∪ deleted` under reverse-deps — so the bus no longer computes it.
- **Delete** the path-shaped path entirely: `from_sync_result` (`:109`) and the
  helpers `_ids_in_files` (`:206`), `_compute_affected` (`:245`),
  `_resolve_files` (`:268`). Keep `_to_relative` only if `touched_files` still
  needs relative normalisation — but prefer to have the native commit emit
  project-relative `touched_files` (Phase 3 metadata) so Python does no path math.
  Reduce `_resolve_layers_from_files` to a small `_layers_touched(delta)` keyed
  off the id fields, not file strings.
- `scoped_to` (`bus/delta.py:44`) and `is_empty` (`:98`) already operate on the
  id sets — leave them, but re-read them against the new field provenance. The
  **file-interest** scoping must keep the delta's files = `touched_files` plus the
  files of affected ids; since `affected_ids` already covers transitive
  dependents and the native delta reports their `touched_files`, confirm
  `touched_files` includes affected-id files (it does, per Phase 3) — if not,
  union them here.
- In `bus/interest.py`, `matches` (`:60`) already does id/file/layer
  intersection with `all` short-circuit. Confirm Phase 6.4's wording holds: an id
  interest matches `affected_ids`; a file interest matches `touched_files` +
  affected-id files; a layer interest matches touched layers; `rescan` matches
  all. The bus already passes `affected_ids=delta.affected,
  affected_files=delta.files, touched_layers=delta.layers` (`bus/bus.py:111-115`)
  — so this is a verification, not a rewrite.

**Verify.**
```bash
devenv shell -- pytest src/tyo3/tests/test_gate8_bus.py -q --no-cov
# This case becomes green; remove its xfail marker once it passes on its own:
devenv shell -- pytest \
  src/tyo3/tests/test_final_bus_contract.py::test_bus_deltas_are_id_level_and_in_revision_order \
  -q --no-cov -rA
```
Remove the `@pytest.mark.xfail` at `test_final_bus_contract.py:107-111` only when
the case passes: the assertion is that every delivered id matches the ULID regex
(`_ULID`, `:26`), i.e. ids are durable, not paths.

> Commit here: `refactor(bus): id-level Delta wrapping the CommitDelta; drop the path→id graph walk`.

---

### Step 6.1 — Centralise the post-commit hook

**What.** Add one method that runs every post-commit step, and route every write
method through it:
```python
def _after_commit(self, delta: CommitDelta) -> None:
    self._invalidate_head_snap()
    self._apply_graph_delta(delta)     # pure applier from Phase 4
    self._schedule_derived(delta)      # id-level invalidation (Phase 7 tightens it)
    self._publish_delta(delta)
```

**Why.** §6.3. The hand-copied per-method tails are why one path (`discard`)
silently skips publishing. Collapsing them into one method makes "every write
runs the same post-commit steps" true by construction.

**Where / how.**
- Add `_after_commit` to `TyO3Session` near the existing post-commit helpers
  (`_publish_delta` `session.py:873`, `_apply_graph_delta` `:1103`,
  `_invalidate_head_snap` `:905`).
  - `_apply_graph_delta(delta)` is already the pure applier wiring (Phase 4): it
    calls `CodeGraph.apply_code_delta(delta.code_delta)` when the head graph is
    materialised and is a no-op otherwise. Keep its no-graph fast path.
  - `_schedule_derived(delta)` is the Phase-7 seam. For Phase 6, point it at the
    **existing** `_invalidate_derived` (`session.py:753`) which already builds the
    dirty set from `created`/`changed` ids and the deleted set from `deleted`
    ids. Do **not** redesign derived invalidation here — Phase 7 owns that. Just
    make sure the hook calls it with the id-level delta.
  - `_publish_delta(delta)` becomes the 6.2 publisher (next step) — for now have
    it build the bus delta via `Delta.from_commit_delta(delta)` (from 6.4).
- **Note the invalidation-order subtlety.** Today each write method calls
  `self._invalidate_head_snap()` *before* the native op (e.g. `session.py:943`),
  and `_apply_graph_delta` calls `_invalidate_derived` internally. Decide one
  order and apply it everywhere through the hook: invalidate the head snapshot
  *after* the commit returns (so the next read re-pins at the new revision),
  apply the graph delta, schedule derived, then publish. The pre-op
  `_invalidate_head_snap()` calls in the write methods are removed — the hook owns
  invalidation. (A stale head-snap cannot be observed between the native publish
  and the hook because writes are serialised through the native commit and the
  session is the only writer.)
- Rewrite each write method to the canonical shape. Example for `edit`
  (`session.py:939-953`):
  ```python
  def edit(self, path: str | StdPath, text: str) -> CommitDelta:
      self._check_open()
      try:
          native = self._inner.edit(str(path), text)
      except _NativeClosedError as e:
          raise ProjectClosedError(str(e)) from e
      except Exception as e:
          raise InternalTyError(f"Unexpected error in edit(): {e}") from e
      delta = CommitDelta.model_validate(native)
      self._after_commit(delta)
      return delta
  ```
  Apply the identical shape to `edit_many` (`:955`), `edit_virtual` (`:970`),
  `sync_path` (`:986`), `discard` (`:1002`), `sync_all` (`:1016`). For
  `poll_changes` (`:1069`) keep the `if native is None: return None` guard
  **before** `_after_commit` so a no-event poll publishes nothing. For `author`
  (`:1223`) see 6.2 (it routes through the hook too, but its graph-delta apply is
  a no-op).
- **Grep to prove centralisation:**
  ```bash
  grep -n "_apply_graph_delta\|_publish_delta\|_invalidate_head_snap" src/tyo3/session.py
  ```
  The only callers inside the write methods should be *inside* `_after_commit`.
  `_invalidate_head_snap` legitimately remains in `reload` (`:1118`), `close`
  (`:1356`), and the snapshot machinery — those are not write commits.

**Verify.**
```bash
devenv shell -- pytest src/tyo3/tests/test_write_path.py -q --no-cov
devenv shell -- pytest src/tyo3/tests/test_gate8_bus.py -q --no-cov
```
The write-path suite must stay green: every method still returns the same
`CommitDelta` and applies the same graph delta — only the *plumbing* changed.

> Commit here: `refactor(session): single _after_commit post-commit hook for every write`.

---

### Step 6.2 — Publish from every write path

**What.** Make every write path publish, including `discard` and `author`. With
the hook from 6.1, `discard` publishing is automatic (it now calls
`_after_commit`); `author` routes through the hook too (its `_apply_graph_delta`
is a no-op because an authored write carries no `code_delta` structure). Then
promote the bus's revision-order check from a warning to an assertion.

**Why.** §5.11 / §6.3. `discard` skipping publish is the headline defect of
`test_every_write_kind_publishes_exactly_one_delta`. And because Phase 5 made
publication the strictly-last in-lock step under one native commit, deltas now
genuinely arrive in revision order — the bus can assert it instead of logging.

**Where / how.**
- **`discard`** (`session.py:1002`): after 6.1 it already calls `_after_commit`,
  which publishes. Confirm there is no remaining early `return` that skips the
  hook.
- **`author`** (`session.py:1223`): route through `_after_commit`. Authored
  writes must **not** apply a code/derived graph delta — keep that property. Two
  clean options:
  - Have `_apply_graph_delta(delta)` early-return when `delta.code_delta` is
    empty / `delta` carries only `authored_ids` (an authored write produces no
    node/edge churn), so the shared hook is safe to call unconditionally; **or**
  - Keep a tiny `_after_authored_commit(delta)` that skips the graph apply but
    still invalidates the head snap and publishes.
  Prefer the first (one hook) so there is genuinely one path. Either way `author`
  ends as `delta = CommitDelta.model_validate(native); self._after_commit(delta);
  return delta`. The bus `Delta` for an authored write carries the
  `authored_ids` and the `authored` layer (so a layer-interested subscriber is
  notified), which `from_commit_delta` already produces (6.4).
- **`_publish_delta`** (`session.py:873`): keep the fast no-op when there are no
  subscribers (`bus is None or not bus.has_subscribers()`), then
  `self._get_bus().publish(Delta.from_commit_delta(delta))`. Drop the old
  `self._head_graph` argument and the `root=` path math — `from_commit_delta`
  needs neither.
- **Promote the order check to an assertion.** In `Bus.publish`
  (`bus/bus.py:92-101`), replace the `logger.error(...)`-and-continue with a hard
  assertion that the revision is strictly greater than the last published one:
  ```python
  assert delta.revision > self._last_published_revision, (
      f"bus received revision {delta.revision} after "
      f"{self._last_published_revision} — revision order broken"
  )
  self._last_published_revision = delta.revision
  ```
  This is now a real guarantee (Phase 5: publication is the commit tail, writers
  serialised). Keep it inside the existing lock acquisition. **Caveat:** if any
  test legitimately publishes the *same* revision twice (it should not after
  Phase 5), fix the caller, not the assertion.
- **Deliver every matched revision, including ALL.** An `Interest.ALL` subscriber
  must receive *every* committed revision (§5.11: "ALL: every committed
  revision"), even one whose scoped delta is empty, so it can pin a snapshot at
  exactly that revision. The current delivery guard
  (`bus/bus.py:117-119`) drops an empty non-rescan scoped delta. Adjust it so a
  matched subscriber always receives: deliver when `delta.rescan`, or the
  interest matched (which for ALL is unconditional), keeping the empty-drop only
  for *scoped* interests where the intersection is genuinely empty. Concretely:
  an ALL or rescan delivery is unconditional; a scoped (ids/files/layers) match
  delivers its non-empty scoped slice. This is what makes
  `test_every_write_kind_publishes_exactly_one_delta` count exactly 1 per write
  (the test subscribes with `Interest.ALL`). Note `sync_all` is a coarse change
  and sets `rescan=True` (Phase 0.3.6), so it is delivered to everyone regardless.

**Verify.**
```bash
devenv shell -- pytest \
  src/tyo3/tests/test_final_bus_contract.py::test_every_write_kind_publishes_exactly_one_delta \
  -q --no-cov -rA
devenv shell -- pytest src/tyo3/tests/test_gate6_authored.py src/tyo3/tests/test_gate8_bus.py -q --no-cov
```
Remove the `@pytest.mark.xfail` at `test_final_bus_contract.py:64-68` once the
"exactly one delta per write kind" case passes on its own. The case exercises
`edit`, `edit_many`, `edit_virtual`, `sync_path`, `discard`, `sync_all`,
`author`, and (via `_inject_changes` + `poll_changes`) the watcher fold — each
must yield exactly one delivered delta.

> Commit here: `fix(session/bus): every write publishes; assert revision order; deliver ALL revisions`.

---

### Step 6.3 — Remove the writer-blocking overflow policy

**What.** Config validation rejects any overflow policy that can block the
writer. The supported policies are all non-blocking:
- `coalesce` — union the affected sets of pending deltas (never hide an
  intervening change),
- `drop_and_mark_lagged` — drop the delta and force the subscriber to rescan,
- `error_and_close` — mark lagged and close the subscription.

**Why.** §5.11: "a slow or dead subscriber MUST NOT stall the writer." The
`"block"` policy's `self._cond.wait()` on the producer side
(`subscription.py:81-87`) is exactly the stall §5.11 forbids.
`test_config_rejects_writer_blocking_overflow_policy` requires a `ConfigError` at
open for `overflow = "block"`.

**Where / how.**
- **Native validation (the test's trigger).** In `rust/src/config.rs`, extend
  `validate` (`:294`) to reject a blocking overflow. The coordination config is
  already parsed into `BusCfg.overflow: String` (`config.rs:436`). Add a check
  that the value is one of the three non-blocking policies and `Err(ConfigError)`
  otherwise. Add a variant such as
  `ConfigError::BlockingOverflow(String)` (or reuse a suitable existing variant
  with a clear message) to the enum (`config.rs:16`) and its `Display`
  (`:29-44`). Because the native config loads at session open and surfaces as
  `tyo3.exceptions.ConfigError`, this makes `TyO3Session(str(root))` raise for
  `overflow = "block"` — which is precisely what the test asserts. **This is the
  one Rust change in Phase 6 → `devenv shell -- build` before the pytest gate.**
- **Accept the renamed policies.** Update the allowed set to `coalesce`,
  `drop_and_mark_lagged`, `error_and_close`. Keep `default_overflow`
  (`config.rs:583`) as `"coalesce"`.
- **Python bus side.** Update the `overflow` `Literal` types and the `_offer`
  policy branches:
  - `Bus.__init__` (`bus/bus.py:31-36`): change the `Literal` to
    `["coalesce", "drop_and_mark_lagged", "error_and_close"]`.
  - `Subscription.__init__` (`subscription.py:42-53`): same `Literal`.
  - `Subscription._offer` (`subscription.py:63-90`): **delete the `"block"`
    branch entirely** (the `self._cond.wait()` producer-side stall). Map the
    branches:
    - `coalesce` → existing `_coalesce_into_tail` (unchanged).
    - `drop_and_mark_lagged` → the old `error` behaviour: set `self._lagged =
      True` and drop the delta (never block). The subscriber detects `lagged` and
      rescans (`rescan_from`, `subscription.py:206`).
    - `error_and_close` → set `self._lagged = True` and `self.close()` the
      subscription (drop + tear down), still never blocking the producer.
  - There is **no** producer-side `_cond.wait()` left. Grep:
    ```bash
    grep -n "_cond.wait" src/tyo3/bus/subscription.py
    ```
    The only `wait` calls must be on the **consumer** side (`poll`, `__next__`).
- **Python coordination-config read.** `_read_coordination_config`
  (`session.py:763`) currently passes `bus_overflow` straight to the `Bus`. It
  swallows exceptions and defaults — leave that facade for now (Phase 10 removes
  the duplicate Python read), but make sure it does not *re-accept* `"block"`:
  since native validation already rejected `"block"` at open, the session never
  reaches bus construction with a blocking policy. If a config sets one of the
  new names, pass it through unchanged.

**Verify.**
```bash
devenv shell -- build   # native ConfigError change must be compiled in
devenv shell -- pytest \
  src/tyo3/tests/test_final_bus_contract.py::test_config_rejects_writer_blocking_overflow_policy \
  src/tyo3/tests/test_final_bus_contract.py::test_slow_subscriber_does_not_block_writer \
  -q --no-cov -rA
devenv shell -- cargo test --manifest-path rust/Cargo.toml config
```
Remove the `@pytest.mark.xfail` at `test_final_bus_contract.py:157-161` once the
rejection case passes. `test_slow_subscriber_does_not_block_writer` (already
green) must **stay** green and fast (it uses `coalesce` with capacity 1 and an
unpolled subscriber, asserting the writer is never stalled).

> Commit here: `fix(bus/config): reject writer-blocking overflow; non-blocking policies only`.

---

## 5. Acceptance — the Phase 6 gate

Run exactly what the plan's Phase 6 "Acceptance" lists, all via devenv:

```bash
devenv shell -- build   # ensure pytest imports the freshly built extension (config change)

devenv shell -- pytest \
  src/tyo3/tests/test_final_bus_contract.py \
  src/tyo3/tests/test_gate8_bus.py \
  -q --no-cov -rA
```

Then the full milestone gate (run the suites in the background; ~10–15 min):

```bash
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria (all must hold):**
- **One post-commit path** — `_after_commit(delta)` is the sole post-commit
  sequence; every write method (`edit`, `edit_many`, `edit_virtual`, `sync_path`,
  `discard`, `sync_all`, `author`, `poll_changes`) routes through it. No write
  method hand-copies `_apply_graph_delta` / `_publish_delta` /
  `_invalidate_head_snap`.
- **Every write publishes** — including `discard` and `author`; a no-event
  `poll_changes` correctly publishes nothing. Proven by
  `test_every_write_kind_publishes_exactly_one_delta` with its xfail removed.
- **Bus deltas are id-level and in revision order** — the `Delta` is a thin
  wrapper over the `CommitDelta`'s id fields; ids are durable (ULIDs), never file
  paths; the bus asserts strictly-increasing revisions. Proven by
  `test_bus_deltas_are_id_level_and_in_revision_order` with its xfail removed.
- **Slow subscribers never stall the writer** — no producer-side blocking remains;
  `test_slow_subscriber_does_not_block_writer` stays green and fast.
- **The blocking overflow policy is rejected** — config validation raises a typed
  `ConfigError` at open for a writer-blocking policy; the supported policies are
  `coalesce`, `drop_and_mark_lagged`, `error_and_close`. Proven by
  `test_config_rejects_writer_blocking_overflow_policy` with its xfail removed.
- **No xfail markers remain** in `test_final_bus_contract.py`; `test_gate8_bus`
  behaviour is preserved over the restructured bus.
- Both full suites stay green.

---

## 6. Pitfalls specific to this phase

- **Forgetting `devenv shell -- build` after the `config.rs` change.** The
  overflow rejection (6.3) is the only native change; a green `cargo test config`
  does not refresh the compiled extension. If
  `test_config_rejects_writer_blocking_overflow_policy` "won't go green no matter
  what you fix," you skipped the rebuild and the test is probing the old
  extension.
- **Removing an xfail marker before the case passes.** Under `xfail-strict`, an
  xfail that *passes* with the marker still on is reported as a failure (XPASS).
  Make the case pass first, then delete the marker — never the reverse.
- **Leaving a second post-commit sequence.** If any write method still calls
  `_publish_delta` or `_apply_graph_delta` directly (not via `_after_commit`),
  the centralisation is incomplete and a future edit can re-introduce the
  `discard` class of bug. Grep and confirm one path.
- **`author` applying a graph delta.** An authored write carries no code/edge
  churn; routing it through `_after_commit` must not mutate the code graph. Make
  the graph apply a no-op for an authored (empty `code_delta`) delta, or keep a
  tiny authored-specific tail — but it must still invalidate the head snap and
  publish.
- **Dropping empty deltas for an ALL subscriber.** `Interest.ALL` means "every
  committed revision." If the delivery guard drops an empty non-rescan scoped
  delta even for ALL, `test_every_write_kind_publishes_exactly_one_delta` counts
  0 for a write that produced no id churn. Make ALL/rescan delivery
  unconditional; keep the empty-drop only for genuinely-empty *scoped* slices.
- **A producer-side `_cond.wait()` surviving.** Deleting the `"block"` *type*
  without deleting the blocking *branch* in `_offer` re-creates the writer stall
  the moment a config (or a default) selects it. Delete the branch; grep for
  `_cond.wait` and confirm only consumer-side waits remain.
- **Reconstructing ids from paths "just in case."** Once `from_commit_delta`
  exists, the graph-walk helpers are dead. Leaving `from_sync_result` and
  `_ids_in_files` around invites a caller to use the lossy path again. Delete
  them.
- **Promoting the order check to an assertion that fires on duplicate revisions.**
  The assertion is `delta.revision > last`. If any path republishes the same
  revision (e.g. a retry, or a test that double-publishes), it trips. After
  Phase 5 each commit publishes once at the tail — if the assertion fires, the
  *caller* is wrong; fix the double-publish, don't relax to `>=`.
- **Over-reaching into Phase 7.** Phase 6 *calls* derived invalidation from the
  hook; it does not fix it. Id-level invalidation precision, read-time staleness,
  and snapshot-lifetime fixes are Phase 7. Keep `_schedule_derived` pointed at
  the existing `_invalidate_derived` and move on.
- **Touching the native commit or the delta shape.** Phase 6 consumes the Phase 3
  `CommitDelta` and the Phase 5 commit unchanged. If you find yourself editing
  `rust/src/project.rs`'s commit or the delta DTO, you have strayed out of scope
  (except the one `config.rs` validation).

---

## 7. Suggested commit sequence for the phase

1. `refactor(bus): id-level Delta wrapping the CommitDelta; drop the path→id graph walk` (6.4)
2. `refactor(session): single _after_commit post-commit hook for every write` (6.1)
3. `fix(session/bus): every write publishes; assert revision order; deliver ALL revisions` (6.2)
4. `fix(bus/config): reject writer-blocking overflow; non-blocking policies only` (6.3)

This maps to the plan's single commit
`fix(session/bus): one post-commit path; publish every write; non-blocking
overflow`; squash on landing if the series is preferred as one reviewed commit.

Every commit passes its focused `pytest` slice; the last one passes the full bus
contract test (all xfail markers removed) and both full suites (the milestone
gate). Remember: **all of it through `devenv shell --`.**

---

## 8. What Phase 6 deliberately leaves for later (so you don't over-reach)

- **Id-level derived invalidation, read-time staleness, snapshot lifetime, typed
  stores** are **Phase 7**. Phase 6 only *calls* derived invalidation from the
  one hook (`_schedule_derived` → the existing `_invalidate_derived`); it does not
  make it precise or fix its snapshot leak.
- **Read-surface / convenience-API lifetime fixes** are **Phase 8** — the bus's
  `rescan_from` (`subscription.py:206`) opens snapshots and is part of the
  lagged-subscriber catch-up; its lifetime hygiene is reviewed alongside the
  read-surface work, not here.
- **Single config source** is **Phase 10.** Phase 6 adds *one* native validation
  (reject the blocking overflow) but leaves the duplicate Python
  `_read_coordination_config` (`session.py:763`) in place; Phase 10 deletes the
  Python re-read and surfaces all coordination settings from the native validated
  config.
- **Splitting `session.py`** (the facade, the views, the post-commit hook, the
  exceptions) and **`bus/`** structure is **Phase 11.** Phase 6 makes the write
  methods thin (`native op → validate → _after_commit → return`), which is exactly
  the seam Phase 11 cuts along — but do **not** move files yet. Keep the change
  behavioural, not structural.

Keeping these out of Phase 6 isolates its single high-value change — one
post-commit path that every write shares, with a bus that carries id-level deltas
in revision order and can never block the writer — behind the Phase 0 bus
contract test, and keeps both suites green at every commit.
