# Phase F implementation guide — CI dep provisioning, hero demo, README, release (proj 28)

Companion to [`CONCEPT.md`](CONCEPT.md) §5.3 (test tiers) and [`PLAN.md`](PLAN.md)
§"Phase F". File-level build spec, written against the **actual post-Phase-E code**
(branch `main`, the cumulative B–E work merged at `91dbf2a`). Where this guide and
PLAN differ, this guide wins.

> **One-line goal:** make the whole single-path stack **reproducible off this
> machine** — provision the curated plugins hermetically so the UI specs
> (`picker`/`ast_nav`/`sidebar`) + the demos run anywhere, wire the Lua specs into
> a devenv entrypoint, re-record the hero loop in the new snacks/sidebar idiom,
> rewrite the README for the single-path `vim.pack` world, and cut a release.
> **This is the "make it portable + ship it" phase — no new product behaviour.**

---

## 0. Where Phase E left things (read this first)

The plugin is feature-complete for proj 28: snacks pickers, always-on LSP bridge,
the code-action act-hub, AST-native nav, and the **edgy accordion sidebar** (Phase
E, just merged). What is **not** done is *portability*:

- **The Lua specs run only by hand.** The canonical invocation is the loop in
  `GUIDE-phase-E.md §5` / `§8`: a pristine `neovim-unwrapped-0.12.2` + a
  treesitter-grammars dir on the rtp, per-spec. **No devenv script runs them**;
  `test-ci` is pytest-only (`devenv.nix:201`). The dep-light specs (`smoke`,
  `lsp*`, `review_dedup`) need no plugins; the UI specs (`picker`, `ast_nav`,
  `sidebar`) `pcall(require, …)` and **skip** when the stack is absent — so today
  CI would silently skip every UI assertion.
- **`demo/pack.lua` is local-only.** It resolves plugins from the developer's
  `~/.local/share/nvim/site/pack/core/opt/<plugin>` (nvim 0.12 `vim.pack` opt dir)
  with a nix-store glob fallback for the two also in nixpkgs (snacks/edgy). On a
  fresh CI box neither path exists. Its own header already names this "the Phase-F
  task."
- **The README is pre-single-path.** It still documents `setup{ lsp = true }`
  (now always-on), the "opt-in/additive/degrades" framing, and an install story
  that predates the `vim.pack` deliverable. Hero GIF is `demo/hero/hero.gif`.

**The stack to provision (the 6 plugins + grammars):** `snacks.nvim`,
`edgy.nvim`, `tiny-code-action.nvim`, `treewalker.nvim`, `nvim-treesitter`,
`nvim-treesitter-textobjects`, plus the **python** treesitter parser (`.so` on the
rtp — `context.lua` + the AST specs need it). All six are present in the dev box's
`vim.pack` opt dir today; the version pins in the user's nvim `init.lua`
`vim.pack` block are the ready-made source-of-truth refs.

**Branch:** `git checkout main && git pull && git checkout -b proj28-phase-f-portability`.

---

## 1. The target

```
devenv.nix                      -- provide the pinned plugin stack + python grammar;
                                   export their store paths; add `test-nvim` + fold
                                   the Lua specs into `test-ci`
editors/tyo3.nvim/tests/
  bootstrap.lua                 -- NEW: put the provisioned stack on the rtp from a
                                   single env signal (shared by specs + demos)
  minimal_init.lua              -- source bootstrap.lua (engine specs still work with
                                   nothing on the rtp)
editors/tyo3.nvim/demo/pack.lua -- resolve from the same env signal first, keep the
                                   local vim.pack/nix fallbacks
editors/tyo3.nvim/demo/hero/    -- re-record the hero loop (snacks + sidebar idiom)
editors/tyo3.nvim/README.md     -- single-path rewrite (vim.pack install, keymaps)
pyproject.toml, rust/Cargo.toml, rust/Cargo.lock, src/tyo3/__init__.py  -- version bump
```

**The unifying idea:** one provisioning source feeds *both* CI specs and demos.
Pick a single env var — `TYO3_NVIM_DEPS` (a `:`-separated list of plugin dirs +
the grammar dir) — that `bootstrap.lua` and `pack.lua` both read. devenv exports
it from Nix-built plugins; locally it's unset and both fall back to the existing
`vim.pack` opt-dir resolution. **No machine-specific paths in committed code.**

---

## 2. Build order

### F.1 — Hermetic dep provisioning + wire the Lua specs (the load-bearing one)

**F.1a — Provide the plugins from Nix (`devenv.nix`).** Add the six plugins +
python grammar to the devenv. ⚠**Decision (provisioning source):**
- **Recommended — nixpkgs `vimPlugins` + pinned `fetchFromGitHub` for the gaps.**
  `snacks.nvim`, `edgy.nvim`, `nvim-treesitter`, `nvim-treesitter-textobjects` are
  in nixpkgs; `treewalker.nvim` and `tiny-code-action.nvim` may not be — build
  those with `pkgs.vimUtils.buildVimPlugin { src = fetchFromGitHub {…pinned rev…} }`
  using the refs from the user's `vim.pack` block. Python grammar via
  `pkgs.vimPlugins.nvim-treesitter.withPlugins (p: [ p.python ])` or the standalone
  `tree-sitter-grammars.tree-sitter-python` (`parser/python.so`).
- Alternative — vendor the six under `tests/deps/` as pinned git checkouts (fetched
  once, cached). Simpler Nix, heavier repo. **Prefer the nixpkgs path** unless a
  plugin is unpackaged *and* annoying to `buildVimPlugin`.

Expose a devenv var, e.g. `env.TYO3_NVIM_DEPS = lib.concatStringsSep ":" [ snacks edgy … grammarDir ];`
(or a `scripts`-level computed string). The store paths are pure; this is the CI
contract.

**F.1b — `tests/bootstrap.lua` (NEW).** A tiny module that reads `TYO3_NVIM_DEPS`
and prepends each entry to the rtp (grammar dir included so parsers resolve). No-op
when the var is unset (engine specs run bare; UI specs skip — the current
behaviour). Keep it dependency-free.

**F.1c — `minimal_init.lua`.** Source `bootstrap.lua` right after adding the plugin
root, *before* `runtime! plugin/tyo3.lua`. Engine specs are unaffected (nothing on
the rtp when the var is unset); UI specs now find the stack when it's set.

**F.1d — `demo/pack.lua`.** In `resolve(name)`, try `TYO3_NVIM_DEPS` entries first
(match by basename), then the existing `vim.pack` opt dir, then the nix glob. One
resolver, two callers, no behaviour change locally.

**F.1e — `devenv.nix` test wiring.** Add `scripts.test-nvim` that runs the spec
loop hermetically: resolve the pristine `neovim-unwrapped` from Nix (not PATH — see
the memory note *nvim-demo-pristine-binary*), set `TYO3_NVIM_DEPS`, loop
`smoke context lsp lsp_nav lsp_codeaction review_dedup lsp_symbols ast_nav sidebar`
(and `picker` if it has a headless spec) with a `timeout`, and **fail the script on
any `N failed` > 0 or `[FAIL]`** (grep the summary line, non-zero exit). Then fold
`test-nvim` into `test-ci` (run after pytest) — or make a `test-all` that runs both,
and point CI at it. Confirm it stays green end-to-end.

### F.2 — Re-record the hero demo (`demo/hero/`)

New loop in the snacks/sidebar idiom (PLAN §F.2): treewalk between functions → the
accordion sidebar expands sections as the cursor moves → `gra`/`<leader>a` opens
the tiny-code-action **buffer picker** → acknowledge review (the ⚠ clears) and/or
Simplify (diff preview). Offline-safe: the **ack** beat needs no LLM; the Simplify
diff can use the offline stub (CONCEPT §1.2). Keep it **< ~800KB, ~35s**. Follow
the **`nvim-demo-record`** skill + **`nvim-demo-review`** (read `hero.txt`
frame-by-frame); set `laststatus=3` in the hero init for the sidebar. Update the
`demo-record-hero` tape/script as needed. Mind the memory note
*nvim-headless-spec-gotchas* (pristine binary, grammar on rtp, `timeout` guards).

### F.3 — README rewrite (`editors/tyo3.nvim/README.md`)

- **Install:** a `vim.pack.add` spec (nvim 0.12 floor) listing the six deps +
  tyo3.nvim; then `require("tyo3").setup{}`. Drop the lazy.nvim/opt-in story.
- **Framing:** single-path philosophy + the **navigate / observe / act** model.
  Remove "opt-in / additive / degrades gracefully" and the `setup{ lsp = true }`
  language (the bridge is always-on now; `lsp` is not a gate).
- **Keymaps:** the defaults — textobjects (`af`/`if`/`ac`/`ic`/`aa`/`ia`),
  treewalker (`<C-k/j/h/l>`), the `gra` action picker, `:TyO3Sidebar`,
  `:TyO3Inspect`→IDENTITY — and the `manage` / `keymaps` opts (incl. `manage =
  false` ⇒ no curated stack, no sidebar).
- **GIFs:** the re-recorded hero + the sidebar GIF; refresh any stale captions.

### F.4 — Release

Per the established flow: bump `pyproject.toml`, `rust/Cargo.toml`,
`rust/Cargo.lock` (the `version =` for the workspace member), `src/tyo3/__init__.py`
(`__version__`). Minor bump (→ `0.6.0` or as appropriate — check the current
version first). Annotated tag `vX.Y.Z`. Commit, tag, push branch + tag. (Whether to
push the tag / open the release is the user's call — confirm before pushing tags.)

---

## 3. Critical facts & decisions

### 3.1 — Engine specs must stay dep-free (don't regress the cheap gate)
`smoke`/`lsp*`/`review_dedup` run today with **nothing** on the rtp. Bootstrap must
be a no-op when `TYO3_NVIM_DEPS` is unset, and `minimal_init.lua` must not hard-
require any curated plugin. The two-tier split (CONCEPT §5.3) is the whole point:
the fast gate runs everywhere; the UI gate runs where the stack is provisioned.

### 3.2 — Resolve the pristine nvim from Nix, never PATH (memory: pristine-binary)
The PATH `nvim` wrapper injects user config even under `--clean`. `test-nvim` and
the demos must resolve `neovim-unwrapped-0.12.2` via the Nix store (the demos
already do; the test script must too). This is the #1 source of headless flakes.

### 3.3 — One resolver, two consumers (don't fork provisioning)
`bootstrap.lua` (specs) and `pack.lua` (demos) read the **same** `TYO3_NVIM_DEPS`.
Don't grow a second mechanism. Local runs (var unset) keep working via the existing
`vim.pack` opt-dir fallback — that's why both keep the fallback.

### 3.4 — UI specs already skip-if-absent; keep it that way
`sidebar.lua`/`ast_nav.lua`/`picker.lua` `pcall(require, …)` and emit a "skipped"
check when the dep is missing. Provisioning *un*-skips them in CI; it must not turn
"absent" into a hard failure (a dev without the stack still runs the dep-light
sweep clean). The `test-nvim` script asserts on `N failed`, not on skips.

### 3.5 — `pinned` refs come from the user's vim.pack block
The version pins for `fetchFromGitHub` are the refs in the user's nvim `init.lua`
`vim.pack` declarations — that's the source of truth that produced the working
demos. Pin to those exact revs so CI matches the recorded behaviour.

### 3.6 — Don't re-open Phase A–E product surface
No new verbs, views, keymaps, or data layers. If a UI spec fails under hermetic
provisioning, the bug is in *provisioning* (rtp order, grammar path, capability
key), not the feature — fix the harness, not the product. If a genuine product bug
surfaces, **flag it** rather than silently patching scope creep.

---

## 4. Definition of done

- `devenv shell -- test-nvim` provisions the stack and runs **all** Lua specs
  green (UI specs no longer skipped); it exits non-zero on any failure.
- `test-ci` runs the Lua specs after pytest (or `test-all` does); CI is green.
- A dev with **no** curated stack still gets the dep-light sweep green (engine
  specs unaffected; UI specs skip cleanly).
- `demo/hero/` re-recorded in the snacks/sidebar idiom (< ~800KB, ~35s), verified
  via `nvim-demo-review`; README points at it.
- README rewritten: `vim.pack` install, navigate/observe/act framing, default
  keymaps + `manage`/`keymaps` opts, no `setup{ lsp = true }`/opt-in language.
- Version bumped across the four files; annotated `vX.Y.Z` tag created (push/tag
  on the user's go).

---

## 5. Verify

```
# Hermetic Lua specs (the new gate):
devenv shell -- test-nvim          # all specs incl. UI, green, non-zero on fail
# Cheap gate still clean with nothing provisioned:
unset TYO3_NVIM_DEPS; <pristine-nvim> --headless --clean -u …/minimal_init.lua \
  -c "luafile …/tests/smoke.lua"   # engine specs pass; UI specs skip
# Daemon unchanged:
devenv shell -- test-fast
# Demo re-render + review:
devenv shell -- demo-record-hero   # then read demo/hero/hero.txt per nvim-demo-review
```

---

## 6. Files you'll touch

- **Add:** `editors/tyo3.nvim/tests/bootstrap.lua`; `demo/hero/` outputs
  (re-recorded gif/txt); `KICKOFF-phase-F.md` is already in scratch.
- **Edit:** `devenv.nix` (plugin provisioning + `TYO3_NVIM_DEPS` + `test-nvim` +
  `test-ci`), `editors/tyo3.nvim/tests/minimal_init.lua`,
  `editors/tyo3.nvim/demo/pack.lua`, `editors/tyo3.nvim/demo/hero/*.tape`/`init.lua`,
  `editors/tyo3.nvim/README.md`, `pyproject.toml`, `rust/Cargo.toml`,
  `rust/Cargo.lock`, `src/tyo3/__init__.py`.
- **Reference (don't change):** the Phase E sidebar code; the existing demo tapes
  (`default/`, `context/`, `sidebar/`) as the recording template.

---

## 7. Out of scope (do NOT start)

- No new product behaviour (verbs/views/keymaps/layers) — Phase F is portability +
  packaging only.
- No CI *pipeline* authoring beyond a green `test-nvim`/`test-ci` unless the user
  asks (GitHub Actions YAML, matrix, caching). Make the entrypoints green and
  reproducible; wiring them into a hosted CI is a separate ask.
- Don't re-record `default/`, `context/`, `sidebar/` unless their content actually
  drifted — only `hero/` is in scope here.

---

## 8. Verification of the UI

- **Primary gate: `test-nvim` green** with the stack provisioned (UI specs run, not
  skipped). That's the proof the hermetic provisioning works.
- **Cheap gate unchanged:** dep-light sweep green with `TYO3_NVIM_DEPS` unset.
- **Hero GIF** reviewed frame-by-frame (`nvim-demo-review`): the treewalk→sidebar-
  accordion→action-picker→ack loop reads cleanly, < ~800KB.

> Mid-build, the thing worth pausing to flag is if a plugin **isn't in nixpkgs and
> resists `buildVimPlugin`** (odd build, runtime deps) — that's the one genuinely
> uncertain provisioning step. If so, fall back to the `tests/deps/` vendored-
> checkout approach for *that* plugin and note it, rather than fighting Nix.
> Everything else — the env-var contract, the one-resolver rule, the README
> rewrite, the release bump — is decided here; execute it.
