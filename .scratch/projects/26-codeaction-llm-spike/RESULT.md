# RESULT — code-action → LLM → durable spine layer (spike)

**Status: landed.** Branch `codeaction-llm-spike` (off `native-lsp-phase2`).
A thin, real, end-to-end vertical slice. Python + Lua + TOML only — **no Rust
change** (it reuses the proj-25 durable-`needs_review` mechanism as-is).

## What's proven end-to-end

A code action on an entity ("tyo3: Explain this entity") runs the daemon
`explain` verb, which **gathers real context** (entity source + where-it's-used
+ existing authored layers), calls the **LLM seam**, and stores the result on a
**durable authored layer keyed by the entity's durable id**. Because it's keyed
by identity on a `review_on_change` layer, the explanation:

- **rides edits and the atomic move** (durable identity), and
- flips to **`needs_review`** when the body later drifts from the version it was
  generated against, **clearing on a re-run** (re-author = acknowledge) — the
  exact mechanism shipped in proj 25 (`AuthoredVersion.reviewed_hash`,
  level-triggered `needs_review`), reused with no engine change.

### The slice, by layer

1. **Daemon `explain` verb** (`src/tyo3/daemon/handlers.py`)
   `explain({path, line, col, mode?})`, all on the actor over one pinned
   snapshot:
   - `did = s.id_for(rel, line, col)` → `null` if nothing resolves;
   - gather context (see `_gather_context`): entity source via
     `tyo3.derive.dag._entity_source`, where-used via
     `s.find_references(..., include_declaration=False)` (capped at 12), and
     every existing authored layer's value for the id (so the LLM sees prior
     human/agent intent). `mode="simplify"` also pulls up to 3 caller bodies
     (resolving each ref site to its enclosing entity via `id_for`);
   - `text = _llm(prompt, system=…)`;
   - `s.author("explain", did, {text, mode, model, generated_at})` — the commit
     that stamps `reviewed_hash`;
   - returns `{durable_id, text, mode}`.
   - Also added **`context_pack`** (same gather, no LLM, no commit) — the
     reusable read substrate for an MCP/agent bridge.

2. **LLM seam** (`src/tyo3/daemon/llm.py`) — `_llm(prompt, *, system)`.
   **Offline deterministic stub by default** (echoes the prompt's `Entity:` line)
   so tests/demo run with no network or key. The real Anthropic call is gated
   behind `TYO3_LLM=anthropic` **and** `ANTHROPIC_API_KEY` **and** an importable
   `anthropic` SDK; otherwise the stub. Real path uses the current Messages API
   shape (cached system block, single user message), default model
   `claude-opus-4-8`, overridable via `TYO3_LLM_MODEL`. `llm_model()` stamps the
   chosen backend onto the stored record (`"stub"` offline).

3. **Plugin code action + command** (`editors/tyo3.nvim/lua/tyo3/lsp.lua`)
   - `CAPS`: `codeActionProvider = true`,
     `executeCommandProvider = { commands = { "tyo3.explain" } }`.
   - `handlers["textDocument/codeAction"]` returns two **client-command**
     actions (Explain / Suggest a simplification) with `{uri,line,col,mode}`
     arguments — **no daemon round-trip to offer them**.
   - `vim.lsp.commands["tyo3.explain"]` (+ `M.run_explain`) resolves the root,
     RPCs the `explain` verb, shows the text in a float, then re-decorates and
     `refresh_layer_diagnostics` so the new record and its future `needs_review`
     surface. Client-command registration means `gra` /
     `vim.lsp.buf.code_action()` / **tiny-code-actions** all drive it for free.

4. **Config** — `[layers.explain]` (`origin="authored"`, `history=true`,
   `review_on_change=true`) declared in the demo/tour config
   (`src/tyo3/demo/tour.py` CONFIG_TOML), which is also the test fixture.

### Tests (hermetic — stub LLM, no network)

- **Daemon pytest** (`src/tyo3/daemon/tests/test_handlers.py`, +9): registration,
  `context_pack` (source + references + existing layers + off-entity null),
  `explain` stores a durable record, `simplify` mode, bad-mode rejection,
  off-entity null, and the **durability tie-in**: author → body edit flips to
  `needs_review`, survives an identical re-commit (save), re-run clears it.
- **Lua headless** (`editors/tyo3.nvim/tests/lsp_codeaction.lua`, 23 checks):
  `textDocument/codeAction` returns the two tyo3 actions with the right command +
  arguments; `run_explain` → durable `explain` record present for the id and
  matching the returned text; simplify mode updates it; **plus the
  extensibility seam** — a registered raw provider + `register_entity_action`
  both surface, and the generic `tyo3.run` dispatcher executes end-to-end.
- All existing suites green: daemon pytest (76), Lua smoke 12 / context 20 /
  lsp 17 / lsp_nav 20 / review_dedup 8, + new lsp_codeaction 23. `ruff` clean;
  full fast suite 718 passed.

## tiny-code-action compatibility + the plugin seam

**tiny-code-action works with this out of the box, and verified why.** Its
`action.apply` runs the chosen action via `client:exec_cmd` / `client:_exec_cmd`
(`tiny-code-action/action.lua`), and Neovim's `exec_cmd` resolves
`vim.lsp.commands[name]` **before** falling back to `workspace/executeCommand`.
Our actions carry a `command` (not an `edit`) and we don't advertise
`resolveProvider`, so tiny-code-action skips `codeAction/resolve` and invokes the
client command directly. No server-side `workspace/executeCommand` handler is
needed (the kickoff's noted alternative); the client-command path is the simpler,
working one. The same holds for `gra` and `vim.lsp.buf.code_action()`.

**The code-action surface is an extensible registry — a spine plugin adds actions
with no fork of `lsp.lua`:**

- `M.register_code_action(provider)` — raw seam. `provider(ctx)` (ctx =
  `{uri,line,col,bufnr}`) returns LSP `CodeAction[]`. The codeAction handler runs
  every provider (each `pcall`-guarded so a bad plugin can't break the menu) and
  concatenates. The built-in explain/simplify is itself just a registered
  provider — the reference example.
- `M.register_command(name, fn)` — register a `vim.lsp.commands` entry so a
  CodeAction's `command` actually runs (the only thing a custom command needs;
  every code-action UI resolves it via `exec_cmd`).
- `M.register_entity_action({title, verb, params?, kind?, on_result?})` — the
  one-call convenience for the 80% case ("call a daemon verb on the entity under
  the cursor, show the text"). It registers a provider emitting one action backed
  by the generic `tyo3.run` dispatcher (`M.run_verb` → daemon → re-decorate +
  refresh layer diagnostics → `on_result`, default a float of `res.text`).

### End-to-end "create your own spine plugin" recipe

Python (the durable spine side, `tyo3.extend`):

```python
from tyo3.extend import register_authored_layer, AuthoredLayerSpec
register_layer(AuthoredLayerSpec(name="security", review_on_change=True))
```

…and (if the capability needs server logic) a daemon verb in
`src/tyo3/daemon/handlers.py` taking `{path,line,col}` — or reuse the existing
generic verbs (`author`, `authored`, `derived`, `explain`, `context_pack`).

Neovim (one call, in the plugin's `setup`):

```lua
require("tyo3.lsp").register_entity_action({
  title  = "tyo3: Find security issues",
  verb   = "explain",            -- or the custom daemon verb
  params = { mode = "explain" },
})
```

That action now appears in **tiny-code-action**, `gra`, and
`vim.lsp.buf.code_action()` for every Python buffer in a tyo3 project, runs the
verb on the resolved durable entity, and (because `run_verb` re-decorates +
refreshes layer-state) surfaces any new layer record and its future
`needs_review`. The `lsp_codeaction.lua` test proves this: a registered raw
provider and a `register_entity_action` both surface in `textDocument/codeAction`,
and the `tyo3.run` dispatcher executes the verb end-to-end.

## Shown off in the recorded demos (multiple ways)

Both vhs recordings now showcase the capability, re-recorded:

- **Default tour** (`demo/default/tour.tape`, Scene 11b): after the native LSP
  bridge is on, `vim.lsp.buf.code_action()` on `checkout` pops the menu with
  **three** tyo3 actions — *Explain this entity*, *Suggest a simplification*, and
  a **custom plugin action** (`myplugin: Draft a docstring`, registered in the
  demo init in one `register_entity_action` call — the extensibility seam on
  screen). Picking Explain floats a model-quality explanation; Simplify shows the
  refactor variant. Verified in `tour.txt`: all three titles + both curated
  outputs render.
- **Context tour** (`demo/context/context.tape`, Scene 2b): the sidebar **ACTIONS**
  pane lists *🤖 Explain (AI)*; activating it stores the summary on the durable
  `explain` layer, which lands in the **NOTES** pane as a 🤖 paragraph glued to
  the entity — then **rides the atomic move** (Scene 5) alongside the note + doc.
  Verified in `context.txt`.

Determinism without a network/key: the demo points the LLM seam at a **curated
offline backend** via `setup.sh` (`TYO3_LLM=callable
TYO3_LLM_CALLABLE=tyo3.demo.explanations:explain`). The `callable` backend
(`src/tyo3/daemon/llm.py`) is a general pluggable seam — `TYO3_LLM=callable` +
`TYO3_LLM_CALLABLE=mod:fn` routes `_llm` at any `f(prompt,*,system)->str` (a real
plugin could point it at its own model wrapper); failures degrade to the stub.
`src/tyo3/demo/explanations.py` holds the curated shop-entity prose. The panel
NOTES pane renders the `explain` record as a wrapped 🤖 paragraph (with the ⚠ on
drift); the ACTIONS pane offers *Explain (AI)* / *Suggest a simplification* when
the project declares the layer (`lua/tyo3/actions.lua`), so the panel is a second,
non-LSP trigger path.

## Trade-off: authored (this spike) vs derived (productionization)

The spike stores on an **authored `review_on_change`** layer. That's the lean
path and is exactly what demonstrates the durability tie-in: the explanation is
**stale-until-re-approved** — drift raises `needs_review`, and a human re-runs
("acknowledge") to clear it. No Rust change.

The richer, **auto-refreshing, non-blocking** version is a **derived** layer
(`tyo3.extend.register_layer(DerivedLayerSpec(name="explain", produce=<llm
generator>, serving="stale", depends_on=("code",)))`, `src/tyo3/extend.py:100`).
`serving="stale"` (AB3) serves the last-good value instantly and recomputes the
LLM call **off the actor**, surfacing fresh via the `derived` bus notification
(the plugin already re-decorates on it). The catch: `review_on_change` is forced
`False` for derived layers (`DerivedLayerSpec.to_layer_config`), so a derived
explanation **auto-regenerates with no human ack** — you lose the explicit
"stale until re-approved" semantics.

**A real feature likely wants both flavours:** a cheap derived auto-summary that
self-heals (content-addressed cache = don't pay twice; AB2 traced read-set =
self-heals on forward/reverse/sibling change) *plus* an ack-gated authored
"deep dive" for load-bearing explanations a human signed off on.

## Other directions (noted, not built)

- **simplify-mode dependency bundling.** The spike caps caller bodies at 3 and
  resolves each ref site to its enclosing entity. A real version would pull the
  transitive forward dependencies too (via the snapshot graph's
  `dependencies()`), bounded by a token budget, and likely use the AB2 recording
  `ProduceContext` (`find_references`/`dependents`/`dependencies`) so the layer's
  cache key tracks exactly what the prompt read.
- **MCP / agents.** `context_pack` is already the read substrate: wrap the daemon
  verbs (`context_pack`, `explain`, `authored`, `review_state`) as MCP tools and
  an agent navigates + annotates by durable id, writing LLM-derived layers as
  durable agent memory on the spine (proj-23 brainstrip,
  `ai-agent-integration-concept`).

## Known limits

- **Entity granularity.** The spine is entity-granular (def/class/method). A
  sub-expression selection (treesitter-textobjects `af`/`ac`, visual mode)
  resolves **up to its enclosing entity** via `id_for` — sub-entity spans are
  not separately addressable (durable-identity binding rules,
  `durable-identity-binding-rules`). The code action consumes the selection's
  `range.start` only; no treesitter-textobjects dependency is needed.
- **Source is read off the snapshot's project root (on disk).** An explanation
  generated after an *unsaved* overlay edit describes the **saved** body. The
  `needs_review`/`reviewed_hash` anchor is the registry content hash (overlay-
  aware), so the *durability* signal is still correct; only the prompt's source
  text lags an unsaved edit. Fine for a spike; a real version would slice the
  overlay text.
- **Renames lose the record** (rename = remove-old + add-new → new id), same as
  every other id-keyed layer; edits and atomic moves preserve it.
