# KICKOFF PROMPT — implement the extensible TyO3 spine

> Paste the block below into a fresh session as its sole brief. It is
> self-contained: it points at the build plan, the supporting context, and the
> memories, and it states the ground rules and the first concrete move. Everything
> it references already exists in the repo.

---

## ⟦ COPY FROM HERE ⟧

You are implementing the **TyO3 spine-extensibility work** — turning the engine
into a backend where a developer can attach arbitrary user-defined data to any AST
node via a programmatic Python API. The full design and a step-by-step build plan
already exist from a prior review engagement. Your job is to **execute the build
plan**, task by task, as a series of PRs.

### 1. Read these first (in order)

All under `.scratch/projects/21-spine-extensibility-review/`:

1. **`IMPLEMENTATION_GUIDE.md`** — your primary document. Phased tasks
   (A quick wins / B registration spine / C powerful+fast), each as
   Goal → Why → Files → Steps (with code skeletons) → Test → Acceptance →
   Landmines → Estimate, plus an 11-PR sequence and contract-test list. **Work
   from this.**
2. `ARCHITECTURE_DELTA.md` — the end-state you are building toward (before→after).
3. `ASSESSMENT.md` — why the codebase is the way it is (evidence-backed, file:line).
4. `API_DESIGN.md` — the concrete registration API (protocols, signatures, examples).
5. `ROADMAP.md` — impact×effort framing and risk table.
6. `SPIKE_FINDINGS.md` — six empirical spikes; **read Spike C (reverse-dep wall)
   and Spike D (rename) carefully** — they shaped tasks AB2 and AB8.

Also read these memories (they encode hard-won, non-obvious truths):
`spine-extensibility-review`, `durable-identity-binding-rules`,
`spine-refactor-v2-plan`, `phase14-acceptance-done`,
`session-reads-via-frozen-snapshot`, `phase8-derived-invalidation-done`,
`devenv-test-entrypoints`, `test-run-timeouts`, `nvim-integration-pr`,
`nvim-context-panel-demos`, `commit-no-ai-attribution`.

### 2. Branch & baseline (do before any code)

- The daemon (`src/tyo3/daemon/`) and plugin (`editors/tyo3.nvim/`) live on branch
  **`nvim-plugin`** (PR #2, still open → `main`). Base your work there:
  `git checkout nvim-plugin && git pull && git checkout -b spine-extend-phase-a`
  (one branch per PR, off `nvim-plugin`, or off `main` once PR #2 merges).
- Establish a green baseline: `devenv shell -- build` then
  `devenv shell -- test-fast`. **Known pre-existing failure**, unrelated to this
  work, do NOT try to "fix" as part of a task:
  `test_gate4_config_sidecar::test_sidecar_is_sole_path_owner_in_source`
  (`demo/tour.py:175` builds a `.tyo3` path the invariant test wants confined to
  `Sidecar`). There may also be a flaky concurrency fixture error. Everything else
  must be green before you start.

### 3. Ground rules (non-negotiable — see IMPLEMENTATION_GUIDE §0.3 and ASSESSMENT §12)

- **Run everything through devenv:** `devenv shell -- build` after any `rust/`
  change; `devenv shell -- test-fast` is your inner loop; `devenv shell -- clippy`
  must be clean for a Rust PR; `devenv shell -- test-final` before merge; daemon
  tests via `devenv shell -- pytest src/tyo3/daemon/tests --no-cov`. Never call
  raw `pytest`/`cargo`. Suites are slow — run full suites in the background,
  budget ~15 min.
- **Golden invariants — do not break:** (1) Rust owns committed truth — new
  authored/derived layers live in Python; never teach Rust new code-layer
  semantics (the only native changes allowed are AB1's authored-layer config shim
  and AB8's explicit rename mutation). (2) Reads run over a frozen snapshot, never
  the live head. (3) Writes stay single-threaded through the `SessionActor`;
  concurrency is added only for reads/async producers. (4) Don't re-tighten the
  parity oracle; don't touch `[profile.dev.package."*"] opt-level=3`.
  (5) Identity: only AB8 changes rename behaviour, and only via an *explicit*
  native rename — never by loosening the durable-id matcher.
- **No AI attribution** anywhere — no "Co-Authored-By", no "Generated with", in
  commits, PRs, code, or docs (memory `commit-no-ai-attribution`).
- **One task ≈ one PR.** Each PR: code + tests + green `test-fast` (+ `build`/
  `clippy` if Rust) + `test-final` before merge. Update the RPC list in
  `editors/tyo3.nvim/docs/dev/architecture.md` whenever you add a verb.

### 4. Order of work (from IMPLEMENTATION_GUIDE §"Suggested PR sequence")

Phase A (quick wins, independent): **QW6 → QW1 → QW3+QW7 → QW4 → QW5.**
Phase B (registration spine): **AB1 (+AB6) → AB5 → AB7.**
Phase C (powerful+fast): **AB2 → AB3 → AB4 → AB8.**

Start with **PR1 = QW6** (fix the renderer status gates — a Lua bug that drops all
derived artifacts from the panel; it unblocks "registered layers surface
everywhere"). Then proceed in order. Don't start a Phase-C task before its Phase-B
dependencies land (AB2/AB3/AB4 assume AB1's registry; AB8 assumes QW1's rename RPC).

### 5. How to work each task

Follow the task's section in `IMPLEMENTATION_GUIDE.md` exactly: touch only the
listed files, implement the steps, write the task's **Test** (the contract tests
are framed as ports of the spikes), and verify against the **Acceptance** criteria
and **Landmines** before opening the PR. A throwaway probe harness is available at
`spikes/run_spikes.py` (and `spike_c2.py`) — copy its project-setup helpers when
you need to exercise the engine by hand. The spikes themselves are throwaway,
untracked, and must not be committed.

### 6. When to pause for human review

- Before **AB1** (the keystone): confirm the registration surface and the
  effective-layer-table merge approach with a maintainer if anything in
  `API_DESIGN.md §3` is ambiguous for the current code.
- Before **AB4** (read concurrency) and **AB8** (native rename): these touch the
  concurrency model and committed-truth semantics respectively. Write the contract
  test FIRST, and pair-review the design. AB8 is explicitly "advanced — pair with a
  maintainer."
- Any time a step forces you to violate a golden invariant: stop and surface it,
  don't work around it.

### 7. Definition of done (the whole effort)

All 14 tasks landed as PRs; the new contract tests (IMPLEMENTATION_GUIDE
§"Cross-cutting") green; `test-final` green; `clippy` clean; the architecture
matches `ARCHITECTURE_DELTA.md` (registration API + traced-read-set producer +
write-actor/read-pool/async daemon + exposed `convert/` verbs + explicit rename +
opt-in schemas + metadata-driven plugin), with every invariant in §3 intact.

Begin by reading the documents in §1, establishing the §2 baseline, then
implementing **QW6**.

## ⟦ COPY TO HERE ⟧

---

## Notes for the human kicking this off (not part of the prompt)

- The deliverables live in `.scratch/` and are **untracked**. If you want the new
  session to rely on them, either keep them in the working tree (they're git-ignored
  scratch, so they persist locally) or move the six design docs into a tracked
  `docs/dev/` location first. The prompt above assumes they're present at the
  `.scratch/projects/21-spine-extensibility-review/` path.
- The spike scripts under `spikes/` are throwaway probes; they import the built
  engine and create temp projects in `/tmp`. They're useful as fixtures but should
  not be committed.
- Realistic effort from the guide's estimates: Phase A ≈ 4–5 days, Phase B ≈ 7–9
  days (AB1 dominates), Phase C ≈ 17–18 days (AB4/AB8 are the long poles). Total
  ≈ 5–6 focused weeks for one engineer; the phases ship value independently.
- Use the `.scratch/projects/22-spine-extensibility-implementation/`path for any and all notes
  or scratch work required during implementation, as well as all progress tracking. 
