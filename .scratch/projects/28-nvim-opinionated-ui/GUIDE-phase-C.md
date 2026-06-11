# Phase C implementation guide — Action hub via tiny-code-action (proj 28)

Companion to [`CONCEPT.md`](CONCEPT.md) §2/§4 and [`PLAN.md`](PLAN.md) §"Phase C".
This guide is the file-level build spec, written against the **actual post-Phase-B
code** (branch `proj28-phase-b-singlepath`). Read it alongside `PLAN.md`'s Phase C
table; where they differ, this guide wins (it reflects what the code really looks
like now).

> **One-line goal:** make the LSP code-action registry the *single* "act on the
> entity" surface, drive it through the tiny-code-action **buffer picker** (default
> keymap), and delete `actions.lua` + the panel ACTIONS pane. Navigation stays
> native (`gd`/`grr`/`K`/`]d`). This is an addition-then-deletion phase.

---

## 0. Where Phase B left things (read this first)

The registry and the buffer-picker config **already exist** — Phase C wires the
remaining actions into them and removes the old catalog. Concretely:

- **`lua/tyo3/lsp.lua` already has the registry seam:**
  - `M.register_code_action(provider)` — `provider(ctx) -> CodeAction[]`.
  - `M.register_command(name, fn)` — registers `vim.lsp.commands[name]`.
  - `M.register_entity_action(spec)` — the one-call convenience (verb on the
    entity → float) backed by the `tyo3.run` dispatcher.
  - The `textDocument/codeAction` handler resolves the entity **once** via
    `entity_at` and builds `ctx = { uri, line, col, bufnr, root, entity,
    diagnostics }`, then calls every provider (sync) and sorts the results
    (preferred first, then title). Providers key off `ctx.entity` and return `{}`
    off-entity.
  - **Built-in providers today:** explain/simplify (entity-gated, *currently
    ungated by layer* — see §3 C.1), and the needs_review acknowledge (`tyo3.ack`,
    `quickfix`, `isPreferred`).
  - **Built-in commands today:** `tyo3.explain`, `tyo3.ack`, `tyo3.run`.
  - `codeActionProvider.codeActionKinds = { "refactor.rewrite", "quickfix",
    "source.tyo3" }`; `executeCommandProvider.commands = { "tyo3.explain",
    "tyo3.run", "tyo3.ack" }`. **You will extend both** (add `refactor.move`;
    add `tyo3.author` / `tyo3.doc` / `tyo3.move`).
  - `codeAction/resolve` already turns a Simplify `data` into a WorkspaceEdit via
    the `simplify_edit` verb (graceful degrade when no rewrite). Keep as-is.
- **`lua/tyo3/deps.lua` already configures tiny-code-action** in
  `setup_tiny_code_action`: `require("tiny-code-action").setup({ picker = {
  "buffer", opts = { hotkeys = true } } })`. **tiny-code-action is NOT installed
  on this machine** (not in the nix store; recorded in `deps.M.missing` →
  `:checkhealth tyo3`). That matters for the demo (see §8).
- **`lua/tyo3/actions.lua` is the catalog to fold + delete.** Its `M.list(card)`
  returns the ACTIONS-pane entries: 🔍 Inspect, 🤖 Explain (AI) + ✨ Suggest a
  simplification (both gated on `has_layer("explain")`), 📝 Author `<layer>` (per
  `note_author_layers()`), 📄 Write/edit doc, ↪ Go to definition, 📞 Find callers,
  🌐 Affected set (picker). It also owns `refresh_layers` / `_authored_layers`
  (the `layers`-verb discovery, `SPECIAL_AUTHORED = { docs, explain }`).
- **`lua/tyo3/panel.lua` renders the ACTIONS pane** (`action_rows(card)` →
  `require("tyo3.actions").list(card)`, the `pane("ACTIONS", …)` call, the
  `run_action` handler in `M.on_line`) and calls `actions.refresh_layers` from
  `set_context`. All of that must stop referencing `actions.lua`.
- **Fold-in targets:** `entitydoc.edit_card(card, src_buf)` (doc editor) and
  `move.move(name, dest)` (atomic move). `picker.affected()` already exists.

**Branch:** `git checkout proj28-phase-b-singlepath && git checkout -b
proj28-phase-c-actionhub` (if B has since merged to `main`, branch off `main`).

---

## 1. The mapping (old ACTIONS entry → new code action)

| Old `actions.list` entry | New code action (provider) | Client command | Kind | Notes |
|---|---|---|---|---|
| 🤖 Explain (AI) | existing explain provider | `tyo3.explain` | `source.tyo3` | **gate on `ctx.layers.explain` present** (DECIDED §3.2) |
| ✨ Suggest a simplification | existing **resolvable** Simplify | `codeAction/resolve` → WorkspaceEdit | `refactor.rewrite` | the prose `mode="simplify"` float is **dropped**; the resolvable rewrite supersedes it |
| 📝 Author `<layer>` | one provider, **one action per writable layer** | `tyo3.author` (prompt → `author`) | `source.tyo3` | writable layers = `layers` verb, `origin=="authored"`, minus `docs`/`explain` |
| 📄 Write / edit doc | "Write/edit doc" | `tyo3.doc` → `entitydoc.edit_card` | `source.tyo3` | |
| (needs_review flagged) | existing acknowledge | `tyo3.ack` | `quickfix` | unchanged |
| Move entity | "Move `<name>` to…" | `tyo3.move` (prompt dest → `move`) | `refactor.move` | name from `ctx.entity.name` |
| 🔍 Inspect / ↪ Go to def / 📞 Find callers | **DROP** | — | — | IDENTITY pane + native `gd`/`grr` cover these |
| 🌐 Affected set (picker) | **not a code action** | — | — | stays a command (`:TyO3Affected`, `picker.affected`) |

Net: the code-action menu on an entity becomes {Author `<layer>`×N, Write doc,
Move, (Explain/Simplify if AI declared), (Acknowledge if needs_review)}.

---

## 2. Build order

### C.1 — Fold `actions.lua` → providers + commands (`lua/tyo3/lsp.lua`)

Add three client commands and the providers that offer them. Put them near the
existing `tyo3.explain`/`tyo3.ack` blocks so the file stays organised.

**Layer discovery → `ctx.layers` (DECIDED — see §3.2 for the full rationale).**
The providers need the project's declared layers when they run. **Fetch the
`layers` verb in the codeAction handler and expose it as `ctx.layers`** — do NOT
build a per-root cache. The handler already resolves `entity_at` once and builds
`ctx`; `ctx.layers` extends that same "resolve once, providers are pure functions
of ctx" pattern. Always-fresh (config edits / runtime `tyo3.extend` registrations
can't go stale), no module-global state, trivially testable.

- **Shape.** After `entity_at` resolves: **if there is no entity, skip the layers
  fetch** (every provider is entity-gated, so off-entity they all return `{}` —
  the round-trip would be wasted). If there is an entity, `daemon_request(root,
  "layers", {}, …)`, then build `ctx = { …, entity = card, layers = <name→descriptor
  map> }` and run the providers. So the menu-open does at most two cheap actor hops
  (`entity_at` + `layers`); the no-op path does one.
- **No cache, no memo.** One extra cheap hop on an explicit, user-initiated
  menu-open is imperceptible (no LLM is involved). If profiling ever shows it,
  add a `{root, head_revision}`-keyed memo — but don't pre-optimize.
- **No `{ "intent" }` fallback.** Offer Author only for layers `ctx.layers`
  actually reports as writable. `session.author` does **not** validate that a
  layer is declared (it writes natively regardless), so offering Author for an
  undeclared layer would silently create an **orphaned record** with no
  `review_on_change` / display config — the exact bug the gate exists to prevent.
- **Writable layers** = `ctx.layers` entries with `origin == "authored"`, minus
  `SPECIAL_AUTHORED = { docs = true, explain = true }` (docs has its own action;
  explain is LLM-generated, never hand-typed).

**`tyo3.author` provider + command.**
- Provider: for `ctx.entity`, emit one CodeAction per writable layer (see the
  writable-layers rule above):
  `{ title = ("tyo3: Author `%s` (%s)"):format(who, layer), kind = "source.tyo3",
     command = { command = "tyo3.author", arguments = { { uri, root, did =
     ctx.entity.durable_id, layer } } } }`.
- **Factor a testable core `M.author_note(bufnr, root, did, layer, value, cb)`**
  that calls `author { layer, durable_id = did, value }` then re-decorates +
  `refresh_layer_diagnostics` (reuse `M.run_verb`'s post-actions). The `tyo3.author`
  *command* is the thin wrapper: resolve `bufnr`/`root`, `vim.ui.input({ prompt =
  layer.." note: " }, …)` (snacks-backed now) → `M.author_note(…, { note = text },
  …)`. Splitting the prompt from the write lets the headless spec call
  `M.author_note` directly (no faking the snacks prompt) — the same split as
  `run_explain` vs the `tyo3.explain` command. This is `actions.author_layer` moved
  in, minus the panel reload.

**`tyo3.doc` provider + command.**
- Provider: `{ title = "tyo3: Write/edit doc", kind = "source.tyo3", command = {
  command = "tyo3.doc", arguments = { { uri, root, did } } } }`.
- Command: resolve the entity card (it's small — re-`entity_at`, or pass enough in
  the args), then `require("tyo3.entitydoc").edit_card(card, bufnr)`. Simplest:
  the command re-resolves via `entity_at` then calls `edit_card`. (Don't depend on
  the panel; `edit_card` opens its own editor.)

**`tyo3.move` provider + command.**
- Provider (only when `ctx.entity` has a name): `{ title = ("tyo3: Move `%s`
  to…"):format(who), kind = "refactor.move", command = { command = "tyo3.move",
  arguments = { { uri, root, name = ctx.entity.name } } } }`.
- Command: `vim.ui.input({ prompt = ("Move %s to (dest path): "):format(name) },
  …)` then `require("tyo3.move").move(name, dest)`. `move.move` already resolves
  the entity in the current buffer and does the atomic move + binds the Moved
  delta; reuse it.

**Capabilities.** Add `"refactor.move"` to `codeActionProvider.codeActionKinds`,
`TYO3_KINDS`, and `executeCommandProvider.commands` gains `"tyo3.author"`,
`"tyo3.doc"`, `"tyo3.move"`.

### C.2 — Default code-action keymap (`lua/tyo3/lsp.lua` attach path + `config.lua`)

- Bind a **buffer-local** keymap on project python buffers that opens the
  tiny-code-action buffer picker: `require("tiny-code-action").code_action()`
  (falls back to `vim.lsp.buf.code_action()` if tiny-code-action is absent — but
  per single-path, prefer to require it and let `:checkhealth` flag absence).
- Where: in `M.attach` (after `vim.lsp.start`) or in `init.on_buf_enter` right
  after the attach call. Guard against double-binding (it's buffer-local, so
  re-binding on re-enter is harmless/idempotent).
- Default key from `config.keymaps` (added empty in B). Suggested default:
  `config.keymaps.code_action = "gra"` (and/or `<leader>a`). Honor `false` to
  opt out, and a user-supplied string to rebind. Document the default.
- ⚠**API** tiny-code-action's entry point is `require("tiny-code-action").code_action(opts?)`
  — verify the function name + signature against the installed version before
  wiring (it's not on this machine yet; see §8). The picker reads code actions
  from the attached LSP clients, so the C.1 providers flow in automatically.

### C.3 — Preview pane (decision, mostly free)

- Simplify already resolves to a diff via `codeAction/resolve` → tiny-code-action
  shows before/after on focus. Nothing to do.
- Informational actions (author/doc/explain/move) have no edit → title + kind
  only. **Default: accept title-only.** Optional enrichment (only if cheap and the
  buffer picker renders it): extend `codeAction/resolve` to attach a tiny preview
  doc for non-edit actions. Defer unless trivial.

### C.4 — Delete `actions.lua` + stop rendering the ACTIONS pane

- `git rm lua/tyo3/actions.lua`.
- **`panel.lua`:** remove `action_rows`, the `pane("ACTIONS", action_rows(...))`
  call, the `run_action` branch in the line handler, and the
  `require("tyo3.actions").refresh_layers` call in `set_context`. Keep the DOCS
  pane (it uses `entitydoc` directly, not `actions.lua`) and IDENTITY/CONTEXT/
  AFFECTED. The panel is still replaced wholesale in Phase E; here you only sever
  the `actions.lua` dependency and drop the now-redundant ACTIONS pane.
- **`picker.lua`** already owns `affected()` (the old "🌐 Affected set" entry);
  nothing to move.
- Sweep: `rg "require\(.tyo3\.actions.\)" lua plugin` must come back empty.

### C.5 — Keep the ex-commands as thin wrappers (single implementation)

- `:TyO3Note` / `:TyO3Doc` / `:TyO3Move` stay as **power-user entry points**, but
  re-pointed at the **same client-command bodies** so there is one implementation:
  - `:TyO3Note [text]` → `notes.note` is fine to keep as-is (it already prompts via
    `vim.ui.input` → snacks); or route it through `tyo3.author`. Keep whichever is
    one code path — don't duplicate the author logic.
  - `:TyO3Doc` → the `tyo3.doc` command body (or keep `entitydoc.edit`).
  - `:TyO3Move <name> <dest>` → `move.move` (the `tyo3.move` body without the
    prompt when args are supplied).
  Single-path governs **UI rendering** (the default surface is the picker), not
  the existence of ex-commands — they just must share one implementation.

---

## 3. Critical facts & decisions

> The two formerly-open questions are now **DECIDED** (3.1, 3.2 below) — the
> engine investigation that settled them is recorded so you can execute, not
> re-deliberate. The rest (3.3–3.5) are guardrails.

### 3.1 — Gate Explain/Simplify on the declared `explain` layer (DECIDED)

**Decision:** gate the Explain/Simplify provider on `ctx.layers.explain` being
present (`origin == "authored"`). It is **on** in the shop test project (which
declares `[layers.explain]`), so this does not hide Explain in `lsp_codeaction`.

**Why (engine facts, verified):**
- Layers are never implicit: `effective_layers = config.toml [layers.*] ∪
  tyo3.extend registrations` (`session._build_effective_layers`). There is no
  built-in `explain`/`intent` — the shop's `config.toml` happens to declare
  `[layers.intent|docs|explain|summary|embed]`.
- `session.author` does **not** validate that a layer is declared — it checks only
  a registered pydantic schema (if any) and then writes natively regardless. So
  the `explain` verb on a project *without* `[layers.explain]` would write an
  **orphaned record** with no `review_on_change` / display config, invisible to the
  `layers` verb — and the needs_review-on-drift story silently breaks.
- Therefore offering Explain where `explain` isn't declared is a latent bug. The
  deleted `actions.lua` already gated on `has_layer("explain")`; the proj-26 LSP
  provider regressed that. **Phase C restores the gate** — correctness, not
  cosmetics. (Same reasoning forbids the `{ "intent" }` Author fallback; see C.1.)

### 3.2 — Source the gate from `ctx.layers`, not a cache (DECIDED)

See C.1 "Layer discovery": fetch the `layers` verb in the codeAction handler
(only when an entity resolved) and expose `ctx.layers`. Providers stay pure
functions of `ctx`; always-fresh; the same data gates Explain/Simplify *and*
drives the per-layer Author actions. **Rejected:** a per-root cache (staleness +
global state to save one cheap, user-initiated round-trip).

### 3.3 — Rewrite `lsp_codeaction.lua` to assert presence, never totals (DECIDED)

Today it asserts `codeAction returns 2 tyo3 actions` (line ~148) and
`registered actions … == 4` — brittle the moment the menu grows. Replace with
**targeted, growth-proof assertions.** From the returned actions derive sets:
`commands` (`action.command.command`), `kinds` (`action.kind`), `data_kinds`
(`action.data.kind`), and `author_layers` (the `layer` arg on each `tyo3.author`
action). Then assert:
- **presence:** `tyo3.explain` (gated — shop declares explain); a Simplify
  (`data.kind == "simplify"`, `kind == "refactor.rewrite"`); `tyo3.doc`;
  `tyo3.move`.
- **`author_layers == { "intent" }`** — the single assertion that proves the whole
  filtering story: `explain`/`docs` excluded from Author (yet `explain` present as
  Explain), `summary`/`embed` excluded as derived. This is the gate-correctness
  proof *without* needing a second project.
- **`kinds ⊇ { source.tyo3, refactor.rewrite, refactor.move }`**.
- off-entity still returns `{}` (keep).
- registered-provider test: assert `tyo3.run` + the sentinel title are present;
  **drop the `== 4`**.
- **end-to-end author:** call the factored core
  `M.author_note(bufnr, root, checkout_id, "intent", { note = … })` directly
  (no faking the snacks prompt — that's why C.1 splits the core from the command),
  then poll `authored("intent", checkout_id)` for the note.

### 3.4 — Guardrails (unchanged)

- **AI stays optional (from B).** Do not let snacks / tiny-code-action / any UI dep
  gate explain/simplify. The LLM backend degradation (anthropic → callable → stub)
  and the Simplify parse-guard fallback are untouched. (The *layer* gate of 3.1 is
  orthogonal — it's about whether the durable sink exists, not about the model.)
- **Don't regress the registry contract.** Third-party `register_entity_action` /
  `register_code_action` must keep working (lsp_codeaction Part 1b/1c asserts a
  registered provider surfaces alongside built-ins and `tyo3.run` dispatches). Add
  your built-ins **through the same `register_*` calls** — don't special-case them
  in the handler.
- **`tyo3.move` is destructive-ish (atomic move rewrites files).** Keep
  `move.move`'s existing safety/notify behavior; the code action only adds the
  prompt + entity-name plumbing. Don't auto-confirm.

---

## 4. Definition of done

- The entity code-action menu offers Author `<layer>` (per writable layer),
  Write/edit doc, and Move — each a client command resolved via
  `vim.lsp.commands`; Explain/Simplify behave per the §3.2 decision; the
  needs_review acknowledge is unchanged.
- `executeCommandProvider.commands` and `codeActionKinds` include the new
  commands/kinds; `refactor.move` is advertised.
- A buffer-local default keymap opens the tiny-code-action buffer picker on
  project python buffers, configurable via `config.keymaps.code_action`.
- `actions.lua` is deleted; `panel.lua` no longer references it and no longer
  renders an ACTIONS pane (`rg "require\(.tyo3\.actions.\)" lua plugin` is empty);
  IDENTITY/CONTEXT/DOCS/AFFECTED still render.
- `:TyO3Note` / `:TyO3Doc` / `:TyO3Move` remain, sharing one implementation with
  the client commands.
- **Dep-light specs green** (the B sweep) and `lsp_codeaction.lua` updated +
  passing for the new providers (incl. an end-to-end `tyo3.author` authors-a-note
  assertion). The tiny-code-action **UI** is verified by the §8 method.

---

## 5. Verify

```
# Dep-light regression sweep (must stay 0 failed). Pristine binary + TS grammar.
PRISTINE=$(echo /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | tr ' ' '\n' | head -1)
GRAMMARS=$(for d in /nix/store/*nvim-treesitter-grammars; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
for t in smoke context lsp lsp_nav lsp_codeaction review_dedup lsp_symbols; do
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean \
    --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/$t.lua" 2>&1 | grep -E "checks, [0-9]+ failed|^\[FAIL\]"
done
# Daemon sanity: devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
```

(`lsp_codeaction.lua` is the primary gate for C — it exercises the providers
headlessly with no UI dep.)

---

## 6. Files you'll touch

- **Edit:** `lua/tyo3/lsp.lua` (providers + commands + caps + keymap + layer
  cache), `lua/tyo3/panel.lua` (drop ACTIONS pane + actions require),
  `lua/tyo3/config.lua` (`keymaps.code_action` default), `plugin/tyo3.lua`
  (`:TyO3*` wrappers if re-pointed), `tests/lsp_codeaction.lua` (new assertions).
- **Delete:** `lua/tyo3/actions.lua`.
- **Reference (don't fold yet):** `lua/tyo3/entitydoc.lua`, `lua/tyo3/move.lua`,
  `lua/tyo3/notes.lua`, `lua/tyo3/picker.lua`.
- **Already done in B (just wire the keymap to it):** `deps.setup_tiny_code_action`.

---

## 7. Out of scope (later phases — do NOT start)

- **Phase D** — AST navigation (treesitter-textobjects + treewalker keymaps,
  `deps.setup_treesitter`/`setup_treewalker` bodies).
- **Phase E** — edgy accordion sidebar (deletes `panel.lua`/`inspect.lua`). In C
  you only stop rendering the ACTIONS pane; the rest of the panel stays.
- **Phase F** — UI-dep test infra, hero re-record, README, release.

---

## 8. Verification of the tiny-code-action UI

The buffer picker is **not headless-testable** (UI-dep test infra is Phase F), but
**the full curated stack IS installed locally** — the user's `vim.pack` checkout at
`~/.local/share/nvim/site/pack/core/opt/` has `snacks.nvim`, **`tiny-code-action.nvim`**,
`edgy.nvim`, `treewalker.nvim`, `nvim-treesitter` (+ textobjects), at the versions
proj-28 targets. (It's NOT in the nix store because `vim.pack` git-clones it; my
earlier "not installed" note was wrong.) Phase B added a shared resolver,
`editors/tyo3.nvim/demo/pack.lua` — `pack.add{ "tiny-code-action.nvim", "snacks.nvim" }`
prepends the real plugins (plugins only, NOT the user's config) to a pristine
demo init's rtp. So:

- **Primary gate (no UI dep): `lsp_codeaction.lua`.** It drives the providers and
  client commands directly through `vim.lsp.buf_request_sync` /
  `vim.lsp.commands[...]` — no picker needed. Make it prove the menu content
  (§3.3) and the `tyo3.author` round-trip. This is sufficient for C's DoD.
- **UI recording (now feasible): a `demo/codeaction/` GIF** driving the **real**
  tiny-code-action buffer picker on an entity (open the menu via the C.2 keymap,
  pick Author/Acknowledge, show the result). Model it on `demo/picker/` (its
  `init.lua` already uses `demo/pack.lua`); `pack.add{ "tiny-code-action.nvim",
  "snacks.nvim" }`. Note tiny-code-action's `picker = "snacks"` mode also works (the
  user runs it that way) — but ship the plan's `picker = { "buffer", … }` default in
  `deps.lua` and record that. Use the repo demo skills.
- **CI portability is the only thing still deferred to Phase F:** the
  `vim.pack`/`demo/pack.lua` path is local-only. Phase F provisions the stack
  hermetically in the project's devenv (pinned vimPlugins; the user's init.lua
  `vim.pack` version pins are the source of truth for the git refs). The local GIF
  doesn't block on that.

Follow the repo skills for any recording: `.agents/skills/nvim-demo-record` (author
the tape; pristine-nvim + single-devenv-sandbox rules) and
`.agents/skills/nvim-demo-review` (read the `.txt` transcript to verify).
