# KICKOFF — continue the extensible TyO3 spine (resume at QW1)

> Paste the block below into a fresh session as its sole brief. PR1 (QW6) is
> already done and in review; this picks up at PR2 (QW1). Everything it references
> exists in the repo.

---

## ⟦ COPY FROM HERE ⟧

You are continuing the **TyO3 spine-extensibility build** — turning the engine into
a backend where a developer can attach arbitrary user-defined data to any AST node
via a programmatic Python API. The design + step-by-step build plan already exist;
your job is to **keep executing the plan, one task per PR**, in order.

### State on entry (already done — do NOT redo)

- **PR1 = QW6 LANDED** (commit `d4791a0` on branch `spine-extend-phase-a`, **PR #3
  open → `nvim-plugin`**): pure-Lua renderer fix so the panel/card render derived
  artifacts (`fresh|stale`) and non-`present` authored records, not just
  `status=="present"`. Lua smoke green 12/12.
- **Baseline is established and understood.** `devenv shell -- build` → exit 0;
  `test-fast` → 679 pass. Two known-bad clusters that are NOT regressions and must
  NOT be "fixed": (a) `test_gate4_config_sidecar::test_sidecar_is_sole_path_owner_in_source`
  (demo/tour.py:175); (b) three `test_concurrency.py` cases that fail only under
  parallel xdist (shared on-disk `fixtures/simple_package`,
  "failed to persist identity registry … No such file or directory") but **pass
  serially** (`-p no:xdist`). Everything else green.

### 1. Read first (in order)

Primary doc: `.scratch/projects/21-spine-extensibility-review/IMPLEMENTATION_GUIDE.md`
— work from it task by task (Goal → Why → Files → Steps → Test → Acceptance →
Landmines). Skim its §0 (env/golden rules) and the **QW1** section in full. Keep
`SPIKE_FINDINGS.md` open (Spike C = reverse-dep wall, Spike E = `find_references`
returns callers — both shape QW1/AB2). Supporting: `ASSESSMENT.md` (§7 = the
stranded `convert/` surface), `API_DESIGN.md`, `ARCHITECTURE_DELTA.md`, `ROADMAP.md`.

Also read these memories: `spine-extensibility-implementation` (proj 22 tracker —
**read its PROGRESS pointer first**), `spine-extensibility-review`,
`durable-identity-binding-rules`, `session-reads-via-frozen-snapshot`,
`phase8-derived-invalidation-done`, `devenv-test-entrypoints`, `test-run-timeouts`,
`nvim-integration-pr`, `nvim-context-panel-demos`, `commit-no-ai-attribution`.

Live progress tracker (untracked scratch, source of truth):
`.scratch/projects/22-spine-extensibility-implementation/PROGRESS.md`. Update it as
you land each PR.

### 2. Branch

PR2 is daemon (Python) + plugin (Lua) + tests — no Rust. Branch off `nvim-plugin`:
`git checkout nvim-plugin && git pull && git checkout -b spine-extend-qw1`
(one branch per PR; base off `main` instead once PR #2 + #3 merge). Re-confirm the
baseline with `devenv shell -- test-fast` before coding if you want, but it's
already characterised above.

### 3. Ground rules (non-negotiable — IMPLEMENTATION_GUIDE §0.3)

- **Everything through devenv:** `devenv shell -- build` after any `rust/` change
  (none for QW1); `devenv shell -- test-fast` is the inner loop; daemon tests via
  `devenv shell -- pytest src/tyo3/daemon/tests --no-cov`; `devenv shell -- test-final`
  before merge. The Lua smoke test must also run **inside** devenv (it spawns bare
  `python`/`python -m tyo3.daemon`, which only resolve in the devenv shell):
  `devenv shell -- nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua -c "luafile editors/tyo3.nvim/tests/smoke.lua"`.
  Never call raw `pytest`/`cargo`. Suites are slow — run full suites in the
  background, budget ~15 min.
- **Golden invariants:** Rust owns committed truth (QW1 adds NO native change);
  reads run over a frozen snapshot via the actor; writes single-threaded through
  `SessionActor`; the new `convert/` verbs are **reads only** — keep them on the
  actor + snapshot path, and do NOT add a *cached* references layer (Spike C:
  cheap-reverse data is a live RPC, never a layer). Positions are **1-based** on the
  wire (`_range_dict`, handlers.py:401).
- **No AI attribution** anywhere (commits, PRs, code, docs).
- **One task = one PR.** Update the RPC list in
  `editors/tyo3.nvim/docs/dev/architecture.md` whenever you add a verb.

### 4. Implement PR2 = QW1 (expose the `convert/` surface as RPC verbs)

Follow IMPLEMENTATION_GUIDE → "TASK QW1" exactly. In short:
1. Add daemon handlers in `src/tyo3/daemon/handlers.py` (follow the `entity_at`
   pattern, handlers.py:129) delegating to the already-built `_ReadOps` methods
   (`read_ops.py`): `references`, `hover`, `can_rename`, `rename` (serialise the
   `WorkspaceEdit` `changes` as `{path: [{range, new_text}]}`), `type_hierarchy`,
   `document_highlights`, `diagnostics_at(path,line,col)` (filter `check_file` diags
   to the position; do NOT cache). Register each in the `_METHODS` dict
   (handlers.py:456).
2. Add daemon tests in `src/tyo3/daemon/tests/test_handlers.py` mirroring Spike E:
   a 2-file project where `references(lib.py, target)` returns the call site in
   `app.py`. Run `devenv shell -- pytest src/tyo3/daemon/tests --no-cov`.
3. Wire the plugin (`editors/tyo3.nvim/lua/tyo3/actions.lua`): replace `goto_def`'s
   `locate` + `vim.fn.search` heuristic with a real references/goto-definition jump,
   and add a "Find callers" action calling `references`. Update `init.lua` if it
   registers verbs.
4. Update `editors/tyo3.nvim/docs/dev/architecture.md` RPC list.

**Acceptance:** the new verbs are reachable over the socket; `ping`'s method list
grows; plugin "find callers" works; daemon tests green; `test-final` green before
merge. **Landmine:** reads only, on the actor + snapshot; no cached references layer.

### 5. After QW1, proceed in order

PR3 = QW3 + QW7 (`layers` / `layer_ids` discovery verbs + generic plugin author
menu). PR4 = QW4 (one shared snapshot per card). Then **PAUSE before AB1** (the
registration-API keystone, PR5) for a human design check per the guide §6. Don't
start any Phase-C task (AB2/AB3/AB4/AB8) before its Phase-B deps land.

### 6. Definition of done for this PR

QW1's verbs exposed + daemon tests + plugin find-callers + `architecture.md`
updated; `test-fast` green; `test-final` green before merge; PROGRESS.md updated;
push the branch and open a PR → `nvim-plugin` (no AI attribution). Then stop and
report, or continue to QW3+QW7 if directed.

## ⟦ COPY TO HERE ⟧
