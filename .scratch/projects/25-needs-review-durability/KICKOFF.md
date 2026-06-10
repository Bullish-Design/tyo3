# KICKOFF — `needs_review` durability (E + A)

You are working in **TyO3** (a Python + Rust/PyO3 "spine" engine: durable code
identity, layered annotations, a daemon, and a Neovim plugin). This task makes the
durable-identity **review state** (`needs_review`) a *persistent, trustworthy*
signal. **Read `.scratch/projects/25-needs-review-durability/DESIGN.md` first** —
it has the verified mechanism, exact `file:line` anchors, edge cases, the
migration, and the full test matrix. This file is the actionable summary.

## The problem (one paragraph)

`needs_review` is computed per-commit by comparing an entity's content hash
against the **immediately prior revision** (`rust/src/identity.rs::reconcile_impl`,
Pass A ~L666 sets it, apply phase ~L790 `set_status(Active)` clears it). So it
means "changed in the most recent commit that reconciled this file" — an **edge
signal**, not durable state. It is cleared by saving (identical re-commit),
editing a *different* function in the same file, or any re-settling reconcile, and
re-authoring the note does **not** clear it (there is no real approve path). The
Phase-2 LSP layer-diagnostic, `authored().status`, and the CLI tour all treat it
as **level state** ("stale until reviewed") — that mismatch is the bug.

## Scope — do BOTH, in this order

### Part E — plugin: stop the redundant double-commit (do first; low-risk, pure Lua)
The editor commits the same overlay text twice — debounced `TextChanged` sync,
then `:w`/BufWritePost sync — an identical re-commit that clears the flag.
- In `editors/tyo3.nvim/lua/tyo3/init.lua`: funnel all sync paths through one
  helper that dedups on a per-buffer text hash (`vim.fn.sha256`). Track
  `M._last_synced[bufnr]`; skip `sync_buffer` when the text hash is unchanged;
  set it on successful sync; seed it in `on_buf_enter` after the first sync.
  Call sites: `buffer_text` (L71), `on_buf_enter` (L77), `on_text_changed` (L102),
  `sync_now` (L132). Leave `sync_buffer` itself unchanged.
- **Test (headless):** add to `editors/tyo3.nvim/tests/` — author an `intent` note
  on `show_label`, edit its body via the debounced sync (flags `needs_review`),
  then trigger a save-equivalent `sync_now` and assert the **revision did not
  advance** and `needs_review` still contains the id. This passes on today's
  engine and must keep passing after A.
- `ruff` is irrelevant (Lua). Run the existing Lua suites (smoke/context/lsp and,
  if present, lsp_nav) to confirm no regression — see "Verification".

### Part A — engine: anchor review-state to the author-time hash (the real fix)
Make review-state a **level** comparison against the body as it was when the note
was authored. **This touches Rust → you must `build`.**

1. **Data model** (`rust/src/authored.rs`): add
   `reviewed_hash: Option<ContentHash>` to `AuthoredVersion` (hex string,
   `#[serde(default)]`). Bump `AUTHORED_FORMAT_VERSION` 1→2 (L15). v1 records load
   with `None` (the existing `from_bytes` only rejects *newer* versions).
2. **Stamp at author time** (`rust/src/project/methods.rs:307 author()` →
   `Mutation::Author` in `commit.rs`, which is `reconcile:false`): capture
   `head.registry.get(&id).map(|a| a.content_hash)` and store it as the new
   version's `reviewed_hash` via `AuthoredStore::put` (`authored.rs:122`). No
   anchor yet ⇒ `None`. Re-authoring re-stamps `= current` (this *is* the
   acknowledge action — closes the "re-author doesn't clear" gap).
3. **Level reads** (the new source of truth, decoupled from the registry status):
   - `needs_review()` (`methods.rs:753`): for each id with a record in a
     `review_on_change=true` authored layer (reuse the "monitored" set from
     `compute_authored_lifecycle`, `commit.rs:209`), flag when the anchor is
     **active** and `reviewed_hash == Some(h)` with `h != anchor.content_hash`.
   - `derive_authored_status()` (`commit.rs:259`): `orphaned` if registry says so
     (unchanged), else `needs_review` if `reviewed_hash.is_some() && !=
     current_anchor_hash`, else `present` (and still `present` for
     `review_on_change=false`).
   - Leave `reconcile_impl`'s `NeedsReview`/`set_status(Active)` as-is (it's now
     irrelevant to review-state but still drives Orphaned). Optional cleanup noted
     in DESIGN §4.6 — **defer it**.
4. **Bus edge-signal unchanged:** `CommitDelta.needs_review` (`commit.rs:340`)
   keeps meaning "changed this commit + has a note" (the notification). Document
   the edge (delta) vs level (query/status) split.
5. **Migration:** `None` ⇒ not flagged (`present`); baseline is set on the next
   author. (Lazy-baseline-on-load is an optional enhancement — DESIGN §4.5.)
6. **Stamp the hash from the registry anchor, never recompute it** (must match
   `Anchor.content_hash` exactly — see DESIGN §8).

## Verification (Definition of Done)

**Engine (A) — Rust unit tests in `rust/src/identity.rs` / `authored.rs` AND a
Python `TyO3Session` test** covering the matrix (DESIGN §7): edit→flagged; save /
identical re-commit→still flagged; edit different func same file→still flagged;
revert body→unflagged; re-author→unflagged; cosmetic edit→never; move (same
hash)→never; `review_on_change=false`→never; orphaned precedence; v1 record loads
→ present. Also assert a v1→v2 format round-trip (load then save) loses no data.

**Consumers (must stay green):**
- `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov` — extend the
  `review_state` handler test to assert persistence across a save-equivalent
  second commit.
- The plugin Lua suites: smoke (12), context (20), lsp (17), and **lsp_nav (20)**
  if present (`nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua -c
  "luafile editors/tyo3.nvim/tests/<spec>.lua"`). The Phase-2 layer-diagnostic
  check in `lsp_nav.lua` should now hold *without* the demo's `:w`-avoidance.
- Full suite green: `devenv shell -- test-fast` (background it; ~15 min).

**Opportunity pass (optional, may defer to a follow-up):** now that saves don't
clear the flag, simplify the default-tour coda
(`editors/tyo3.nvim/demo/default/tour.tape` + `record_cast.py`) to use `:w`
normally and drop the forced-refresh nudge; consider a `:TyO3Approve` command and
a "diff since reviewed" view. **Don't fold these into the core PR** unless cheap.

## State on entry

- **Branch base:** PR #14 (`native-lsp-phase2`) is the branch carrying the
  Phase-2 LSP layer-diagnostic and `tests/lsp_nav.lua` that consume `needs_review`.
  If #14 has **merged to `main`**, branch off `main`. If **not merged**, branch off
  `native-lsp-phase2` so you can verify the consumer (`lsp_nav.lua`) against your
  engine change. Confirm `editors/tyo3.nvim/tests/lsp_nav.lua` exists before
  relying on it; if it's absent, branch off `main` and verify via the daemon
  `review_state` test instead.
- Independent in-flight work: do not touch other open PRs.
- **A needs a Rust build.** Use `devenv shell -- build` (debug) after Rust edits,
  before running Python/Lua that exercises the native extension.

## Ground rules

- **Everything through devenv** (Nix). Never run raw `pytest`/`cargo`. Build:
  `devenv shell -- build`. Tests: `test-fast` (parallel, no-cov) for the suite;
  `pytest … -q --no-cov` for a targeted module; `test-rust`/`check-rust`/`clippy`
  for the Rust side. Suites are slow — **background long runs, budget ~15 min**.
- `ruff check` any touched Python. `clippy` clean (`-D warnings`) on touched Rust.
- **No AI attribution anywhere** (commits, PR, code, docs). End commit messages
  with no Co-Authored-By / "Generated with" trailers.
- Keep the change cohesive: E and A can be one PR (E is a prerequisite-quality fix)
  or two — your call; if one PR, structure commits so E lands first.
- Update docs that describe review-state semantics: the daemon/README protocol
  note for `review_state` (proj 24), and any "needs_review" prose in
  `editors/tyo3.nvim/README.md` / `docs/dev/`. Make the edge-vs-level distinction
  explicit.

## First moves

1. Read `.scratch/projects/25-needs-review-durability/DESIGN.md` end to end.
2. Recall memories: `needs-review-cleared-by-noop-recommit`,
   `native-lsp-bridge-shipped`, `durable-identity-binding-rules`,
   `spine-refactor-v2-plan` ("Rust owns committed truth"),
   `devenv-test-entrypoints`, `test-run-timeouts`, `commit-no-ai-attribution`.
3. Ship **E** + its headless test; verify the save case passes and no Lua
   regressions. Commit.
4. Implement **A** bottom-up (data model → stamping → reads → migration), `build`,
   then the Rust + Python test matrix. Re-verify the consumers (daemon
   `review_state`, `lsp_nav.lua`, smoke/context/lsp), run `test-fast` in the
   background, open the PR. Update the `needs-review-cleared-by-noop-recommit`
   memory to record the resolution.

## Done when
E removes the double-commit (revision doesn't advance on a no-op save) and A makes
`needs_review` survive saves, same-file edits, restart, revert-correctly, and
re-author-clears — with the Rust+Python matrix, daemon, and Lua suites green, and
the docs updated.
