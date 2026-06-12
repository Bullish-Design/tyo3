# Phase F kickoff — CI dep provisioning, hero demo, README, release (proj 28)

You're implementing **Phase F** of the tyo3.nvim opinionated-UI redesign. The
full file-level spec is **[`GUIDE-phase-F.md`](GUIDE-phase-F.md)** — read it first,
it's the contract. This kickoff is orientation + the few judgement calls.

## What's done (Phases A–E, on `main` @ `91dbf2a`)
snacks pickers, always-on LSP bridge, the tiny-code-action **act-hub**, AST-native
nav, and the **edgy accordion sidebar**. The plugin is feature-complete for proj 28.
What's missing is **portability**: the curated stack is only resolvable on the dev
box, the Lua UI specs only run by hand, the README predates the single-path /
`vim.pack` world, and there's no release cut.

## Your job (four sub-phases — see the guide for file-level detail)
1. **F.1 — Hermetic provisioning + spec wiring.** Provide the 6 plugins + python
   grammar from Nix; export one env var (`TYO3_NVIM_DEPS`) that a new
   `tests/bootstrap.lua` *and* `demo/pack.lua` both consume; add a `test-nvim`
   devenv script that runs every Lua spec green and **fails on any failure**; fold
   it into `test-ci`. *This is the load-bearing sub-phase.*
2. **F.2 — Re-record `demo/hero/`** in the snacks/sidebar idiom (treewalk → sidebar
   accordion → action picker → ack). Offline-safe, < ~800KB, ~35s.
3. **F.3 — README rewrite** for `vim.pack` install + navigate/observe/act framing +
   default keymaps; drop the `setup{ lsp = true }`/opt-in language.
4. **F.4 — Release**: version bump across `pyproject.toml` / `rust/Cargo.toml` /
   `rust/Cargo.lock` / `src/tyo3/__init__.py`; annotated `vX.Y.Z` tag.

## Setup
```
git checkout main && git pull && git checkout -b proj28-phase-f-portability
```

## Decisions already made (don't re-litigate)
- **One provisioning source, two consumers** — specs and demos read the same
  `TYO3_NVIM_DEPS`; locally-unset falls back to the existing `vim.pack` opt-dir
  resolution. Don't fork it.
- **Engine specs stay dep-free** — bootstrap is a no-op when the var is unset; the
  cheap gate must keep running everywhere. UI specs keep their skip-if-absent.
- **Pristine nvim from Nix, never PATH** (memory: *nvim-demo-pristine-binary*).
- **Provisioning source:** nixpkgs `vimPlugins` where packaged + pinned
  `fetchFromGitHub`/`buildVimPlugin` for the gaps (treewalker / tiny-code-action),
  using the user's `vim.pack` refs as the pins. Vendor under `tests/deps/` only if
  a plugin resists `buildVimPlugin`.
- **No new product behaviour.** If a UI spec fails under hermetic provisioning, the
  bug is in the harness (rtp/grammar/capability), not the feature.

## The one thing to pause and flag
If a needed plugin **isn't in nixpkgs and resists `buildVimPlugin`** (weird build,
runtime deps), take the `tests/deps/` vendored-checkout fallback for *that* plugin
and note it — don't burn the session fighting Nix. Everything else is decided in
the guide; execute it.

## Definition of done
`devenv shell -- test-nvim` green (UI specs run, not skipped) and non-zero on
failure; `test-ci` includes it; dep-light sweep still green with no stack; `hero/`
re-recorded + reviewed; README rewritten; version bumped + tagged (push/tag on the
user's go). Per-sub-phase commits. Update the proj-28 memory pointer when done.
