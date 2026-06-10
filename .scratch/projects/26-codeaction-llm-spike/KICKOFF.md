# KICKOFF — code-action → LLM → durable spine layer (a spike)

You are working in **TyO3**, a Python + Rust/PyO3 "spine" engine: durable code
identity, layered annotations (authored + derived), a JSON-RPC daemon
(`src/tyo3/daemon/`), and a Neovim plugin with an opt-in native LSP bridge
(`editors/tyo3.nvim/`). This task is a **spike**: a thin, real, end-to-end
vertical slice — not a production feature. The goal is to prove the shape of
"act on whatever is under the cursor/selection, run an LLM over it, and store the
result *durably on the spine* so it survives edits and knows when it goes stale."

Read this whole file first. It is self-contained; the anchors below are verified.

## The vision (why this matters)

The spine already resolves a buffer position → a **durable entity id** and carries
human notes + derived artifacts per id. If we add an LSP **code action** ("tyo3:
Explain this", "tyo3: Suggest a simplification") that resolves the selection to an
entity, runs an LLM with real context (the entity source + where-it's-used + any
existing notes), and writes the result back as a **layer keyed by the durable id**,
then the AI output is no longer a throwaway popup. Because it's keyed by identity:

- it **rides edits and the atomic move** (durable identity), and
- on a `review_on_change` authored layer it shows **`needs_review`** when the body
  drifts from when the explanation was generated — clearing when you re-run it.
  That reuses the durability mechanism just shipped in proj 25
  (`AuthoredVersion.reviewed_hash`, level-triggered `needs_review`).

This is the concrete first step of the proj-23 brainstorm
(`.scratch/projects/23-ai-agent-integration/`, memory
`ai-agent-integration-concept`): MCP/agents reading & writing LLM-derived layers
as durable agent memory on the spine.

## Architecture decision (do it this way)

**The LLM call lives in the daemon (Python), not the editor.** That's where the
Python SDKs, the session actor, the read surface, and (eventually) the derived-layer
producers live. The editor's code action is just the **trigger**; it RPCs a new
daemon verb that does context-gathering → LLM → store, then refreshes decorations.

**Store the result on an *authored* `review_on_change` layer** (written via the
existing `author` verb). This is the lean path and directly demonstrates the
needs_review durability tie-in — **no Rust change required**. (The richer
"auto-refreshing, non-blocking" alternative — a *derived* layer with
`serving="stale"` and a generator that calls the LLM — is described in §"Productionization";
do NOT build it for the spike, just note it in the write-up.)

## Scope — build this vertical slice

### 1. Daemon: an `explain` verb (`src/tyo3/daemon/handlers.py`)
Add a `Handlers.explain` method and register it in the dispatch table
(`_METHODS`, ~line 771 — the `"review_state": Handlers.review_state,` block).

Signature: `explain({ path, line, col, mode? })` (1-based line/col; `mode` ∈
`{"explain","simplify"}`, default `"explain"`). Steps, all on the actor over one
pinned snapshot (golden rules #2/#3 — see how `entity_at`/`review_state` do it):
1. Resolve the entity: `did = s.id_for(rel, line, col)`; return an error/`null` if
   `None`. Get its node (name, kind, qualified_name, range, source slice) the way
   `decorate`/`entity_at` (handlers.py:133, :154) read the graph node.
2. Gather context for the prompt:
   - the entity's **source text** (slice the node range out of the buffer/overlay,
     or read the file),
   - **where-used**: `s.find_references(rel, line, col, include_declaration=False)`
     (the `references` verb at handlers.py:321 shows the shape),
   - **existing layers** on the id (notes/summaries) via the same per-id reads
     `_entity_dict` uses (handlers.py:577) — so the LLM sees prior human intent.
   - For `mode="simplify"`, also pull the bodies of the direct references (the
     "and all its dependencies / where-used" the user asked for). Keep it bounded
     (cap the number of reference bodies).
3. Call the LLM seam `_llm(prompt) -> str` (see §2). Build a focused prompt from
   the gathered context.
4. **Store durably**: `s.author("explain", did, {"text": ..., "mode": mode,
   "model": ..., "generated_at": ...})`. This stamps `reviewed_hash` at author
   time (proj 25) → the record goes `needs_review` when the body later drifts.
5. Return `{ "durable_id": did, "text": ..., "mode": mode }`.

Optional (nice, not required): factor the gathering into a `context_pack` verb
that returns `{ source, references, layers }` for an id — reusable by agents/MCP.

### 2. The LLM seam (hermetic by default)
Put a small `_llm(prompt: str, *, system: str | None = None) -> str` in a new
`src/tyo3/daemon/llm.py` (or inline). **Default to an offline deterministic stub**
so tests run with no network/keys: e.g. return a templated string echoing the
entity name/kind + a canned sentence. Gate the real call behind an env var
(`TYO3_LLM=anthropic` + `ANTHROPIC_API_KEY` present and the `anthropic` SDK
importable); when off, use the stub. For the real call, **use the `claude-api`
skill** (it enforces prompt caching and current model ids — default to a current
Claude model, e.g. `claude-opus-4-8` or `claude-sonnet-4-6`). Tests must never hit
the network — assert against the stub.

### 3. Plugin: code-action + command (`editors/tyo3.nvim/lua/tyo3/lsp.lua`)
- Advertise in `CAPS` (lsp.lua:27): `codeActionProvider = true` and
  `executeCommandProvider = { commands = { "tyo3.explain" } }`.
- Add `handlers["textDocument/codeAction"]` (mirror the existing
  `handlers["textDocument/references"]` etc., and the `_server` dispatch at
  lsp.lua:379-415). It receives `{ textDocument.uri, range, context }`; convert
  `range.start` with `lsp_pos_to_daemon` (lsp.lua:54) and return a small
  `CodeAction[]`:
  ```lua
  { { title = "tyo3: Explain this entity", kind = "refactor",
      command = { title = "...", command = "tyo3.explain",
                  arguments = { { uri = uri, line = L, col = C, mode = "explain" } } } },
    { title = "tyo3: Suggest a simplification", ... mode = "simplify" } }
  ```
- Register the command client-side: `vim.lsp.commands["tyo3.explain"] = function(cmd, ctx) ... end`
  — pull `arguments[1]`, resolve the root, `daemon_request(root, "explain", {...},
  cb)` (use the existing `daemon_request` helper), then on success show the text
  (a float / the panel) and re-decorate + `refresh_layer_diagnostics` so the new
  `explain` record (and its future `needs_review`) surfaces. Keeping it a client
  command means **tiny-code-actions / `gra` / `vim.lsp.buf.code_action()` all
  drive it for free** — the user's `tiny-code-actions` ask is satisfied by simply
  returning code actions; no hard dependency on that plugin.
- (Alternative, note only: instead of a client command, return the action with no
  client handler so nvim sends `workspace/executeCommand` to the server, and add
  `handlers["workspace/executeCommand"]`. Client-command is simpler for the spike.)

### 4. Config: declare the `explain` layer
Add an authored, monitored layer so `author("explain", …)` is accepted and drift
shows as `needs_review`. In the **demo/tour config** and your **test configs**:
```toml
[layers.explain]
origin           = "authored"
history          = true
review_on_change = true
```
(Model: `[layers.intent]` in `src/tyo3/demo/tour.py` CONFIG_TOML and in
`src/tyo3/tests/test_needs_review_durability.py`.) Programmatic alternative to
mention in the write-up: `tyo3.extend.register_authored_layer` /
`register_layer(AuthoredLayerSpec(name="explain", review_on_change=True))`
(`src/tyo3/extend.py`) — for a real feature you'd register it, not hand-edit TOML.

### 5. Tests (hermetic — stub LLM)
- **Daemon pytest** (`src/tyo3/daemon/tests/test_handlers.py`, model on the
  `review_state`/`author` tests): build the shop project (or a tiny one) with the
  `explain` layer; call `handlers.explain({path,line,col})`; assert it returns
  text and that `handlers.authored({layer:"explain", durable_id})` now has the
  value with `status == "present"`. Then `sync_buffer` a body edit and assert
  `review_state`/`authored` reports the `explain` record as `needs_review`
  (durable). Re-run `explain` → back to `present` (re-author = acknowledge).
- **Lua headless** (`editors/tyo3.nvim/tests/`, model on `lsp_nav.lua`): with
  `lsp=true`, `vim.lsp.buf_request_sync(bufnr, "textDocument/codeAction", params)`
  returns the two tyo3 actions; invoke `vim.lsp.commands["tyo3.explain"]` (or the
  daemon `explain` verb directly) and assert the `explain` record exists for the
  entity's id.
- Keep the existing Lua suites green (smoke 12 / context 20 / lsp 17 / lsp_nav 20 /
  review_dedup 8) and `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov`.

## treesitter-textobjects (the user's AST-navigation ask)
No bundling needed. treesitter-textobjects is editor-side selection (`af`/`ac`/…);
the code action just consumes the resulting **range** and resolves it to an entity
via `id_for`. The spine is **entity-granular** (def/class/method) — a sub-expression
selection resolves up to its enclosing entity (the durable-identity binding rules,
memory `durable-identity-binding-rules`). Note this limit in the write-up; don't
try to make sub-entity selections addressable.

## Productionization (write up, don't build)
The durable, *auto-refreshing*, non-blocking version is a **derived** layer:
`tyo3.extend.register_layer(DerivedLayerSpec(name="explain", produce=<llm
generator>, serving="stale", depends_on=("code",)))` (`src/tyo3/extend.py:100`).
`serving="stale"` (AB3) serves the last-good value instantly and recomputes the
LLM call off the actor, surfacing fresh via the `derived` bus notification (the
plugin already re-decorates on it). Trade-off vs the spike's authored layer:
derived auto-regenerates (no human ack, `review_on_change` is forced `False` for
derived — see `DerivedLayerSpec.to_layer_config`), whereas the authored layer
gives the explicit "stale until re-approved" `needs_review` semantics. A real
feature likely wants *both* flavours (a cheap auto summary + an ack-gated deep
dive). Capture this in the write-up.

## Anchors (verified)
- Daemon verbs + dispatch table: `src/tyo3/daemon/handlers.py` — `entity_at`:133,
  `decorate`:154, `author`:197, `authored`:211, `references`:321, `definition`:343,
  `review_state`:538, `_entity_dict`:577, `_METHODS` table:~771. Session reads:
  `s.id_for/locate/find_references/author/snapshot`.
- LSP server: `editors/tyo3.nvim/lua/tyo3/lsp.lua` — `CAPS`:27, conversions:54-67,
  `daemon_request`/`path_to_uri` helpers, handler table + `_server` dispatch:379-415,
  `refresh_layer_diagnostics`:~489, layer-state messages:475-480.
- Durable needs_review (just shipped, reuse — no change): `rust/src/authored.rs`
  (`reviewed_hash`, `AUTHORED_FORMAT_VERSION=2`), `rust/src/project/commit.rs`
  (`needs_review_ids`, `derive_authored_status`); memory
  `needs-review-cleared-by-noop-recommit`.
- Extend API: `src/tyo3/extend.py` — `AuthoredLayerSpec`:54, `DerivedLayerSpec`:100,
  `register_layer`:234, `register_authored_layer` (the native shim).
- Config model: `src/tyo3/demo/tour.py` CONFIG_TOML `[layers.intent]`.

## Ground rules
- **Everything through devenv** (Nix): `devenv shell -- pytest … -q --no-cov` for a
  module; `devenv shell -- test-fast` (parallel, no-cov; background it, ~15 min) for
  the suite; Lua via `nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua
  -c "luafile editors/tyo3.nvim/tests/<spec>.lua"`. **No Rust build needed** for the
  spike (Python + Lua + TOML only) — only run `devenv shell -- build` if you take the
  derived-producer path. `ruff check` any touched Python; `stylua`/existing Lua style.
- **No AI attribution anywhere** (commits, PR, code, docs) — no Co-Authored-By /
  "Generated with" trailers.
- Keep it a **spike**: smallest real slice, clearly labelled, tests hermetic
  (stub LLM, no network). Don't gold-plate; leave a short write-up of what's proven
  and the productionization path (derived/`serving=stale`, MCP/agents, simplify-mode
  dependency bundling).
- This is dual-use/benign (a dev-tool LLM helper in an authorized repo) — fine to build.

## Branch base
- Branch **off `native-lsp-phase2`** — the native LSP bridge (`lsp.lua`), the
  `author`/`review_state` daemon verbs, and the durable needs_review all live there
  (PR #14 lineage, not yet merged to `main`). If `native-lsp-phase2` has since
  merged to `main`, branch off `main`. Confirm `editors/tyo3.nvim/lua/tyo3/lsp.lua`
  has the `CAPS` table and `_server` before relying on these anchors.
- Don't touch other open PRs/branches.

## First moves
1. Recall memories: `ai-agent-integration-concept`, `native-lsp-bridge-shipped`,
   `needs-review-cleared-by-noop-recommit`, `durable-identity-binding-rules`,
   `spine-extensibility-implementation`, `devenv-test-entrypoints`,
   `test-run-timeouts`, `commit-no-ai-attribution`.
2. Skim `handlers.py` (`entity_at`, `references`, `author`, `review_state`,
   `_entity_dict`, `_METHODS`) and `lsp.lua` (`CAPS`, `_server`, a handler,
   `daemon_request`). Skim `src/tyo3/extend.py` specs.
3. Build bottom-up: `explain` verb + stub `_llm` → daemon test green → codeAction +
   `tyo3.explain` command in `lsp.lua` → Lua test → declare `[layers.explain]` in the
   demo/test configs. Verify the needs_review-on-drift tie-in. Keep all existing
   suites green.
4. Short write-up (`.scratch/projects/26-codeaction-llm-spike/RESULT.md`): what's
   proven end-to-end, the authored-vs-derived trade-off, the simplify-mode and
   MCP/agent directions, and the entity-granularity limit. Update the
   `ai-agent-integration-concept` memory with the spike outcome.

## Done when
A code action on an entity ("tyo3: Explain this") runs the daemon `explain` verb,
which gathers real context (source + references + existing layers), calls the LLM
seam (stub in tests), and stores the result on the durable `explain` layer keyed by
the entity's id — visible in the editor, surviving edits/move, and flipping to
`needs_review` when the body drifts (clearing on re-run). Daemon + Lua tests green
(hermetic), existing suites unbroken, write-up + memory updated.
