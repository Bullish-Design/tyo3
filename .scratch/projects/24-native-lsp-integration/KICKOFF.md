# KICKOFF — Native LSP bridge for tyo3.nvim (`vim.lsp` + `vim.diagnostic`)

You are working in **TyO3** (Python + Rust spine engine) on its Neovim plugin,
`editors/tyo3.nvim`. This task builds a **native LSP integration**: the plugin's
code-intelligence surface (hover, references, rename, document highlight,
diagnostics) should ride Neovim's *native* `vim.lsp` / `vim.diagnostic` machinery
instead of bespoke floats/pickers — so it inherits the user's existing config
(`K` hover, `grr`/`gra` maps, `]d`/`[d`, Trouble, lualine diagnostics, fzf-lua /
snacks / telescope LSP pickers) for free.

> **This is a prototype.** Ship a coherent, tested first slice; leave clearly
> marked stretch items for a follow-up. Do NOT rip out the existing bespoke UI —
> the LSP path is **additive and opt-in** (`setup{ lsp = true }`).

---

## Why this is the right move (context)

The daemon **already exposes** the LSP-shaped verbs over its JSON-RPC socket
(`hover`, `references`, `document_highlights`, `type_hierarchy`, `can_rename`,
`rename`, `diagnostics_at`, `check`). But the plugin uses **zero** `vim.lsp` and
**zero** `vim.diagnostic` today — it re-implements hover as a custom float,
references via a custom telescope picker, etc. Mapping the existing verbs to
native primitives is mostly **wiring + position/range conversion**, and it makes
~60% of the plugin feel native overnight and ride the user's own keymaps/plugins.

The spine-specific features (durable identity, authored/derived layers, affected
closure) have no LSP vocabulary and **stay bespoke** (panel + virtual text). A
*later* phase can surface layer state as a `vim.diagnostic` namespace
(`needs_review`/`stale`) so `]d`/Trouble navigate it too — that is **out of scope**
here, noted as a follow-up.

Related brainstorm (read for the bigger picture, not required):
`.scratch/projects/23-ai-agent-integration/AI_AGENT_INTEGRATION_CONCEPT.md`
(the MCP-bridge idea reuses the same verb surface).

---

## State on entry

- **Base branch:** `main` @ `709b52c` (== `origin/main`). **Branch off `main`.**
- **Independent in-flight work — do NOT touch:** PR #12 on branch
  `derived-notify-e2e-demo` (daemon `derived`-notification e2e + async pop-in
  demo). It is unrelated; leave it alone.
- **No Rust required.** This is pure-Lua plugin work plus, at most, *small*
  additive daemon handler tweaks if a verb shape needs adjusting (avoid if
  possible). No `build`.
- **nvim version available here: 0.12.2** — supports the in-process LSP server
  pattern (`vim.lsp.start{ cmd = function(dispatchers) ... end }`). Use it.

### The plugin's existing plumbing (reuse, don't reinvent)
`editors/tyo3.nvim/lua/tyo3/`:
- `rpc.lua` — JSON-RPC 2.0 client over a unix pipe (newline-delimited frames).
- `daemon.lua` — per-project daemon lifecycle; `M.find_root(path)`,
  `M.ensure(root, cb)` → ready client. One daemon+client per project root.
- `init.lua` — `M.rpc(bufnr, method, params, cb)` and `M.with_client(bufnr, cb,
  on_err)` are the call surface. Buffer lifecycle: `on_buf_enter` (open +
  sync_buffer + decorate), `on_text_changed` (debounced sync), `sync_now`.
  `handle_notification(root, method, params)` routes `delta`/`derived`/
  `refinement`.
- `config.lua` — defaults table merged with `setup{}` via `vim.tbl_deep_extend`.
  Add the opt-in flag here.
- `decorate.lua`, `panel.lua`, `card.lua`, `inspect.lua`, `context.lua`,
  `telescope.lua`, `actions.lua`, `notes.lua`, `docs.lua` — the bespoke UI
  (leave intact).
- `plugin/tyo3.lua` — user commands + autocmds.
- `tests/minimal_init.lua`, `tests/smoke.lua` (12 checks), `tests/context.lua`
  (20 checks) — the headless harness to mirror for the new test.

---

## The daemon verbs you will bridge (exact shapes)

All defined in `src/tyo3/daemon/handlers.py`. **Positions IN are 1-based**
(`line`, `col`); **ranges OUT are 1-based** `{start:{line,column}, end:{line,column}}`;
**paths OUT are project-relative posix** (e.g. `"store.py"`). Params accept an
absolute path too (handler relativizes via `_relpath`).

| Verb | Params | Result |
|---|---|---|
| `hover` | `{path, line, col}` | `HoverResult.model_dump` or `null`: `{location:{path, range}, contents:[{kind, value}]}` where `kind ∈ {type,signature,docstring,typed_dict_key,markdown,plain_text}` |
| `references` | `{path, line, col, include_declaration=true}` | `{references:[{path, range, kind}]}`, `kind ∈ {read,write,other}` |
| `document_highlights` | `{path, line, col}` | `{highlights:[{path, range, kind}]}` |
| `can_rename` | `{path, line, col}` | `{can_rename:bool, range:{...}|null}` |
| `rename` | `{path, line, col, new_name}` | `{new_name, changes:{relpath:[{range, new_text}]}}` or `null` |
| `type_hierarchy` | `{path, line, col}` | `TypeHierarchy.model_dump` or `null` (see `models/navigation.py:110+`) **[STRETCH]** |
| `check` | `{path?}` | `{diagnostics:[{file, range, severity, code, message, details}], count}` ; `severity ∈ {fatal,error,warning,information,hint}` (StrEnum string) |

**No goto-definition verb exists.** Either don't advertise `definitionProvider`,
or add a `definition` daemon verb (small, additive — `models/navigation.py:70`
`DefinitionTarget` is the shape; check `read_ops`/`_ReadOps` for an existing
`goto_definition`/`definition` method before adding). Recommend: **skip in the
prototype, note as a follow-up.**

### Severity / kind mappings
- DiagnosticSeverity → LSP: `fatal`→1, `error`→1, `warning`→2, `information`→3, `hint`→4.
- ReferenceKind → DocumentHighlightKind: `read`→2, `write`→3, `other`→1.

---

## Recommended architecture: an in-process LSP server bridging to the daemon

Implement `lua/tyo3/lsp.lua`. On attach to a Python buffer in a TyO3 project (when
`config.get().lsp`), call:

```lua
vim.lsp.start({
  name = "tyo3",
  root_dir = root,
  cmd = function(dispatchers)
    return require("tyo3.lsp")._server(root, dispatchers)
  end,
}, { bufnr = bufnr })
```

`vim.lsp.start` dedupes by `{name, root_dir}`, so one in-process server per
project is reused across buffers. The `cmd`-as-function returns a server object
(an in-process RPC endpoint — **no separate process, no Content-Length framing**):

```lua
return {
  request = function(method, params, callback, notify_reply_callback)
    -- dispatch on method; eventually call callback(err, result) (async OK).
    -- return ok:boolean, request_id:integer
  end,
  notify = function(method, params) return true end,   -- didOpen/didChange/exit/...
  is_closing = function() return closed end,
  terminate = function() closed = true end,
}
```

Inside `request`, call the daemon via the existing client. Get the daemon client
for `root` (mirror how `daemon.ensure`/`M.with_client` resolve it), fire the verb,
and call the LSP `callback(nil, result)` when it returns. The RPC is async and its
callbacks land on the main loop already.

### Methods to implement (prototype scope)
- `initialize` → `callback(nil, { capabilities = CAPS, serverInfo = {name="tyo3"} })`
- `initialized`, `textDocument/didOpen|didChange|didClose` (notify) → no-op
  (the plugin's own `on_buf_enter`/`on_text_changed` already sync buffers to the
  daemon; see the staleness note below)
- `shutdown` → `callback(nil, nil)`; `exit` (notify) → `terminate()`
- `textDocument/hover` → `hover` → `{ contents = { kind = "markdown", value = <joined contents> }, range = <lsp range> }`
- `textDocument/references` → `references` (read `params.context.includeDeclaration`) → `Location[]`
- `textDocument/documentHighlight` → `document_highlights` → `DocumentHighlight[]`
- `textDocument/prepareRename` → `can_rename` → `Range | null`
- `textDocument/rename` → `rename` → `WorkspaceEdit { changes = { [uri] = TextEdit[] } }`
- `textDocument/diagnostic` (pull, LSP 3.17) → `check {path}` → `{ kind = "full", items = Diagnostic[] }`

`CAPS` (advertise only what you implement):
```lua
{
  positionEncoding = "utf-8",   -- SEE position-encoding note; verify empirically
  hoverProvider = true,
  referencesProvider = true,
  documentHighlightProvider = true,
  renameProvider = { prepareProvider = true },
  diagnosticProvider = { interFileDependencies = false, workspaceDiagnostics = false },
}
```

### Conversion helpers (the fiddly core — get these right)
- `lsp_pos → daemon`: `{ line = pos.line + 1, col = pos.character + 1 }`
- `daemon_range → lsp_range`: subtract 1 from every line/column.
  **Nuance:** daemon ranges read as 1-based *inclusive*; LSP end is *exclusive*.
  For hover/highlight/references display this is usually fine; for **rename**
  `TextEdit` precision, verify the end column maps correctly (a rename that
  clobbers one extra char or misses one is the tell). Add a focused test.
- `uri → daemon path`: `vim.uri_to_fname(params.textDocument.uri)` → pass the
  absolute path (daemon relativizes).
- `daemon relpath → uri`: join `root .. "/" .. relpath` → `vim.uri_from_fname`.

### Position encoding
The daemon's column semantics (utf-8 byte vs utf-16 vs codepoint) are **not
documented here — verify empirically** with a non-ASCII fixture. Advertise the
matching `positionEncoding` so Neovim doesn't double-convert. For the prototype,
ASCII fixtures (the shop project) are safe; **flag non-ASCII correctness as a
follow-up** and pick `utf-8` unless the test says otherwise.

---

## Buffer-sync staleness (important subtlety)

The plugin already pushes buffer text to the daemon on `BufEnter` (`open` +
`sync_buffer`) and on debounced `TextChanged`. The LSP `did*` notifications can
therefore be no-ops. BUT a hover/references issued *between* an edit and the
debounce fire reads slightly stale daemon state. Options:
1. **Prototype:** rely on existing sync; document the window. Simplest.
2. **Better:** on an LSP request, `sync_now(bufnr)` first (or have the LSP
   `didChange` push `sync_buffer`), then issue the verb. Costs a round-trip.

Recommend (1) for the first cut; note (2). Do **not** double-sync on every
keystroke.

---

## Wiring checklist

1. `config.lua` — add `lsp = false` to `M.defaults` (opt-in; document it).
2. `lua/tyo3/lsp.lua` — the in-process server + converters + `attach(bufnr, root)`.
3. `init.lua` `on_buf_enter` — after the existing open/sync, if
   `config.get().lsp` and filetype is python and root resolved, call
   `require("tyo3.lsp").attach(bufnr, root)`. (Attach is idempotent via
   `vim.lsp.start` dedupe.)
4. Optionally a `:TyO3Lsp` toggle command + `:checkhealth` line. (Nice-to-have.)
5. Keep everything behind the flag so default behaviour is unchanged.

---

## Verification (Definition of Done)

- **New headless test** `editors/tyo3.nvim/tests/lsp.lua` (mirror `tests/context.lua`
  setup: build the shop project via `tyo3.demo.tour._build_project`, `setup{ lsp =
  true, daemon_cmd = {"python","-m","tyo3.daemon"} }`, open `store.py`). Assert:
  - the `tyo3` LSP client attaches (`vim.lsp.get_clients{ name="tyo3", bufnr=… }`),
  - `vim.lsp.buf_request_sync(bufnr, "textDocument/hover", params, 5000)` on
    `checkout` returns non-empty `contents`,
  - `textDocument/references` returns ≥1 `Location`,
  - `textDocument/documentHighlight` returns ≥1 highlight,
  - `textDocument/prepareRename` + `textDocument/rename` produce a `WorkspaceEdit`
    with a `TextEdit` whose range maps back to the symbol exactly,
  - pull `textDocument/diagnostic` returns a `{kind="full", items=…}` report.
  Run: `devenv shell -- bash -c 'nvim --headless -u
  editors/tyo3.nvim/tests/minimal_init.lua -c "luafile
  editors/tyo3.nvim/tests/lsp.lua"'`. Print `PASS/FAIL` per check, `cquit 1` on any
  failure (match the existing harness).
- **No regressions:** existing Lua smoke **12/12** and context **20/20** still
  green (LSP is opt-in, so default-off runs must be untouched). `devenv shell --
  test-fast` at baseline if any Python touched.
- `ruff check` on any touched Python; keep Lua tidy/consistent with the codebase.
- Re-record demos **only if** you add an LSP scene (optional; not required).

---

## Scope boundary

**In (prototype):** hover, references, documentHighlight, prepareRename + rename,
pull diagnostics; opt-in `lsp=true`; headless test; no regressions.

**Out (explicit follow-ups — note them in the PR, don't build):**
- goto-definition (needs a daemon `definition` verb or a references-based shim).
- `type_hierarchy` → LSP `prepareTypeHierarchy`/supertypes/subtypes (more shape work).
- Surfacing **spine layer state** (`needs_review`/`stale`/derived `computing`) as a
  `vim.diagnostic` namespace so `]d`/Trouble navigate it (the high-value "go
  native for the bespoke half" phase — its own PR).
- Non-ASCII position-encoding correctness.
- Push diagnostics (`publishDiagnostics`) on bus deltas vs the pull model.

---

## Ground rules

- **Everything through devenv.** Pure-Lua (+ maybe tiny additive daemon tweaks);
  **no `build`** (no Rust). Suites are slow — background long runs, budget ~15 min.
- Branch off **`main`** (`709b52c`); open a PR to `main`, marked a **feature
  prototype**. The `derived-notify-e2e-demo` PR #12 is independent — don't touch it.
- **No AI attribution** anywhere (commits, PR, code, docs).
- Additive + opt-in: default-off behaviour and the existing bespoke UI must be
  unchanged.

## Memories to recall
`nvim-integration-pr`, `nvim-context-panel-demos`, `nvim-demo-pristine-binary`,
`devenv-test-entrypoints`, `test-run-timeouts`, `commit-no-ai-attribution`,
`feedback-tools-are-examples`.

## First moves for the new session
1. Read `editors/tyo3.nvim/lua/tyo3/{init,rpc,daemon,config}.lua` and
   `src/tyo3/daemon/handlers.py` (the verb shapes above).
2. Confirm the in-process `vim.lsp.start{cmd=function}` contract on nvim 0.12
   (`:h vim.lsp.start`, `:h vim.lsp.rpc`, the `dispatchers`/server-object shape).
3. Build `lsp.lua` with `initialize` + `hover` first; smoke it end-to-end
   (`vim.lsp.buf_request_sync` hover on `checkout`) before adding the rest.
4. Layer in references → documentHighlight → rename → pull diagnostics, testing
   each. Then write `tests/lsp.lua`, verify no regressions, open the PR.
