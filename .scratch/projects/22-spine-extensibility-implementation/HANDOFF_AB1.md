# Fresh-context kickoff — TyO3 spine-extensibility, PR5 = AB1 (registration keystone)

> Paste this as the opening prompt of a new session. Phase A (the four quick wins)
> is done and in review; the next task is the **registration API keystone, AB1**,
> which the plan gates behind a **human design review before any code is written**.

---

You are continuing the **TyO3 spine-extensibility build** — turning the engine into
a backend where a developer attaches arbitrary user-defined data to any AST node via
a programmatic Python API. The design + 11-PR build plan already exist; your job is
to keep executing them **one task per PR, in order** — but **AB1 is special: stop and
get a human design sign-off before coding** (it is the centerpiece and the one task
that touches committed-truth invariants + adds a native shim).

### State on entry (Phase A complete — do NOT redo)

All four Phase-A quick wins are **landed-in-review**, each its own branch + PR →
`nvim-plugin` (which itself is PR #2 → `main`, still open). Nothing is merged yet;
the PRs stack as siblings off `nvim-plugin`:

| PR | Task | Branch | What it did |
|----|------|--------|-------------|
| #3 | QW6 | `spine-extend-phase-a` | Lua renderer: panel/card show derived (`fresh|stale`) + non-`present` authored records |
| #4 | QW1 | `spine-extend-qw1` | daemon RPC verbs `references`/`document_highlights`/`hover`/`type_hierarchy`/`can_rename`/`rename`/`diagnostics_at` (reads only, live); plugin "Find callers" |
| #5 | QW3+QW7 | `spine-extend-qw3-qw7` | `layers` + `layer_ids(layer,with_values?)` verbs; ACTIONS author menu built from discovered authored layers; picker uses one `layer_ids` hop |
| #6 | QW4 | `spine-extend-qw4` | `entity_at`/`decorate` each open ONE snapshot for the whole cross-layer card (was K) |

**QW5 was deferred and folded into AB1** (it needs a `display` field; AB1 carries
`display` through registration rather than a standalone Rust `LayerCfg` change).

**Baseline (stable, characterised — NOT regressions):** `devenv shell -- build` → 0;
`test-fast` → ~681–682 pass. The only persistent failure is
`test_gate4_config_sidecar::test_sidecar_is_sole_path_owner_in_source`
(`demo/tour.py:175` builds a `.tyo3` path the invariant wants confined to Sidecar).
Plus a flaky cluster in `test_concurrency.py` that fails ONLY under parallel xdist
(shared on-disk `fixtures/simple_package`, "failed to persist identity registry …
No such file or directory") and **passes serially** (`-p no:xdist`). Treat both as
environment, never "fix" them. Daemon tests run separately (not in `test-fast`):
`devenv shell -- pytest src/tyo3/daemon/tests --no-cov` (currently 36+ green).

### ⛔ Before any AB1 code: the mandatory design review

AB1 is the keystone (guide estimate 4–5 days) and the only Phase-B/C task that:
- adds a **native shim** (`PyTyProject.register_authored_layer` in `rust/src/project/
  methods.rs` + a synthesized `LayerCfg{origin:Authored}` into the in-memory
  `ValidatedConfig` in `rust/src/config.rs`) — config only, never new code semantics;
- moves built-in generator/store dispatch **through** new process-global registries
  (a refactor that must stay behaviour-identical);
- fixes the layer table **at open** (golden rule), so registration must run *before* open.

**Do this first, then STOP and present it to the human for sign-off:**
1. Read the AB1 plan end to end (see below) and write a short design note covering:
   the registry data model (`_LAYERS`/`_GENERATORS`/`_STORES` + the spec dataclasses
   from API_DESIGN §2.2/§2.3/§2.5), how built-ins resolve *through* the registry
   (the `make_generator`/`open_store` refactor), how registered layers merge with the
   validated native config (the `effective_layers` projection), where `load_plugins()`
   entry-point discovery hooks in, and the exact shape + safety of the native
   authored-layer shim.
2. Call out the open decisions for the human: (a) is the native shim acceptable, or
   should registered-authored writability be solved another way? (b) `effective_layers`
   surface — thin projection vs something richer? (c) AB6 store-registry + the
   `_OPTIONAL_BACKENDS` replacement + the fs-store GC no-op — fold into AB1 or split?
   (d) does QW5's `display` ride in on the spec here?
3. Use `EnterPlanMode` / present the note in chat. **Do not write AB1 code until the
   human approves the approach.**

### 1. Read first (in order)

Primary: `.scratch/projects/21-spine-extensibility-review/IMPLEMENTATION_GUIDE.md`
→ **TASK AB1** (and skim §0 golden rules). Then `API_DESIGN.md` §1 (registration
surface), §2.2/§2.3/§2.5 (the spec dataclasses you copy), §3 (how registration
reaches the engine), §6 (migration from config strings), §5.1/§5.4 (worked authored +
derived examples to port into the test). Context: `ASSESSMENT.md` §4 (no registration
path today: custom generator *types* need editing `make_generator`; store *backends*
need editing `open_store`). Keep `SPIKE_FINDINGS.md` handy (Spikes A/B = custom layers
via config alone — the AB1 test ports them).

Live tracker (untracked, source of truth):
`.scratch/projects/22-spine-extensibility-implementation/PROGRESS.md` — read its
"PAUSE POINT" section + the PR1–PR4 writeups. Update it as you go.

Memories to recall: `spine-extensibility-implementation` (proj-22 tracker),
`spine-extensibility-review`, `durable-identity-binding-rules`,
`session-reads-via-frozen-snapshot`, `phase8-derived-invalidation-done`,
`devenv-test-entrypoints`, `test-run-timeouts`, `commit-no-ai-attribution`,
`nvim-integration-pr`.

### 2. Branch

After the design is approved: `git checkout nvim-plugin && git pull &&
git checkout -b spine-extend-ab1`. (Keep basing PRs off `nvim-plugin` as siblings
until PRs #2–#6 merge; switch to `main` once they do.)

### 3. Ground rules (non-negotiable — IMPLEMENTATION_GUIDE §0.3)

- **Everything through devenv.** `build` after ANY `rust/` change (AB1 has one),
  then `test-fast`; `clippy` (-D warnings) must be clean for the Rust shim;
  daemon tests via `pytest src/tyo3/daemon/tests --no-cov`; `test-final` before merge.
  Suites are slow — run full suites in the background, budget ~15 min. The Lua smoke
  test must run **inside** devenv (`nvim --headless --clean -u
  editors/tyo3.nvim/tests/minimal_init.lua -c "luafile editors/tyo3.nvim/tests/smoke.lua"`).
- **Golden invariants:** Rust owns committed truth — the native shim adds *config*,
  never new *code-layer semantics* (rule #1). Reads over a frozen snapshot (rule #2).
  One writer via `SessionActor` (rule #3). Don't re-tighten the parity oracle or touch
  `[profile.dev.package."*"] opt-level=3` (rule #4). Identity: edit ✅ / atomic move ✅
  / rename ❌ (rule #5) — AB1 does not change this.
- **Registration runs before `open()` freezes the layer table** — `load_plugins()` is
  called once, lazily, at the top of `TyO3Session.__init__` (module-flag guarded), and
  `session.__init__` calls `register_authored_layer` for each registered authored spec
  after open. Keep `make_generator`/`open_store` back-compatible (pure refactor:
  `test-fast` must stay green at step 1 before you add anything new).
- **No AI attribution** anywhere (commits, PRs, code, docs). End nothing with
  Co-Authored-By / "Generated with".

### 4. Implement AB1 (+AB6) — follow IMPLEMENTATION_GUIDE "TASK AB1" exactly

New `src/tyo3/extend.py` (registries + `register_layer/register_generator/
register_store` + `load_plugins()`); refactor `derive/{generators,dag}.py` and
`stores/__init__.py` so built-ins dispatch through the registries; thin
`session.effective_layers` (native ∪ registered) consumed by `handlers._entity_dict`,
the `layers` verb (QW3), and the card loops; the one native shim
(`methods.rs::register_authored_layer` + `config.rs`); fold in AB6 (store registry,
replace `_OPTIONAL_BACKENDS`, finish the fs-store `_gc_store` no-op `dag.py:355`).

**Test** (`src/tyo3/tests/test_registration.py`): register a custom authored `tests`
layer + a derived `complexity` layer *from the test* (no config file), open a
session, author+read `tests`, derive `complexity`, assert both ride the `entity_at`
card (port Spikes A/B); assert a dup registration raises; assert an entry-point
plugin loads; assert built-ins stay green. Also add the daemon-side `layers` entry
for a registered layer.

**Acceptance:** a custom authored + derived layer works end-to-end **registered in
Python, zero config file, zero core dispatch edits**; built-ins unchanged.

### 5. Definition of done for PR5

Design note approved by the human → `extend.py` + refactors + native shim + AB6 +
`test_registration.py`; `devenv shell -- build` → 0; `clippy` clean; `test-fast`
green (baseline failures only); daemon tests green; `test-final` green before merge;
`architecture.md` updated if any verb shape changes; PROGRESS.md + the proj-22 memory
updated; push `spine-extend-ab1` and open a PR → `nvim-plugin` (no AI attribution).
Then stop and report.

### 6. After AB1

PR6 = AB5 (optional per-layer schemas). PR7 = AB7 (per-layer subscriptions). Then
Phase C: AB2 (producer + traced read-set) → AB3 (async serve) → AB4 (read
concurrency — **write the test first**) → AB8 (native rename rebind — **pair with a
maintainer**; touches committed truth). Don't start a Phase-C task before its Phase-B
deps land. The guide's "Suggested PR sequence" table is the master order.
