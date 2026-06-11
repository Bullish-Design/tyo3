# tyo3.nvim — Implementation Guide

Companion to [`REVIEW.md`](REVIEW.md). This turns every finding into concrete,
ordered changes with file paths, code, test additions, and verification commands.

**Conventions used throughout**
- All project commands run inside the devenv shell. Build first, then test:
  ```
  devenv shell -- build
  devenv shell -- test-fast            # parallel, no-cov
  ```
- Headless Lua specs (the canonical plugin proof) run under devenv too:
  ```
  devenv shell -- nvim --headless --clean \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/<spec>.lua"
  ```
- Daemon pytest: `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov`
- Demos: `devenv shell -- demo-record` / `demo-record-context`

**Phase order** (each phase is independently shippable; later phases assume earlier ones):

| Phase | Item(s) | Surface | Risk |
|---|---|---|---|
| 1 | B2 rpc timeout | Lua | low |
| 2 | F.1–F.4 code-action UX | Lua + existing verbs | low |
| 3 | A1/A2 hero demo loop | tape only | none |
| 4 | B3/B4/C cleanups + README | Lua + docs | low |
| 5 | B1 responsive `explain` | Python (server + handler) | medium |
| 6 | F.5 resolvable Simplify → diff preview | Python + Lua | medium |
| 7 | B5/D polish | Lua | low |

---

## Phase 1 — Per-request timeout in `rpc.lua` (B2)

**Goal:** a stalled daemon request errors its callback instead of hanging forever.

### 1.1 `lua/tyo3/config.lua` — add the default

In `M.defaults`, add:

```lua
  -- Per-request timeout (ms) for daemon RPCs. A stalled request errors its
  -- callback instead of hanging the UI forever. Slow verbs (explain/check) can
  -- override per-call. Set 0 to disable.
  request_timeout_ms = 20000,
```

### 1.2 `lua/tyo3/rpc.lua` — arm/disarm a timer per pending id

In `M.new(opts)`, carry the timeout and a timer table:

```lua
  return setmetatable({
    socket = opts.socket,
    on_notification = opts.on_notification,
    on_close = opts.on_close,
    timeout_ms = opts.timeout_ms or 0,
    pipe = nil,
    connected = false,
    next_id = 0,
    pending = {},
    timers = {},          -- id -> uv_timer (armed request deadlines)
    buffer = "",
  }, Client)
```

Add a private helper near the top of the `Client` methods:

```lua
function Client:_clear_timer(id)
  local t = self.timers[id]
  if t then
    self.timers[id] = nil
    pcall(function()
      t:stop()
      t:close()
    end)
  end
end
```

In `Client:_dispatch`, clear the timer the moment a response for that id arrives —
just before resolving the callback:

```lua
    if obj.id ~= nil then
      local cb = self.pending[obj.id]
      self.pending[obj.id] = nil
      self:_clear_timer(obj.id)              -- ← add
      if cb then
        ...
      end
    end
```

In `Client:request`, arm a timer after registering the callback. Accept an optional
`opts.timeout_ms` override:

```lua
function Client:request(method, params, cb, opts)
  if not self.connected or not self.pipe then
    ...
    return
  end
  self.next_id = self.next_id + 1
  local id = self.next_id
  if cb then
    self.pending[id] = cb
    local tmo = (opts and opts.timeout_ms) or self.timeout_ms
    if tmo and tmo > 0 then
      local uv = vim.uv or vim.loop
      local timer = uv.new_timer()
      self.timers[id] = timer
      timer:start(tmo, 0, vim.schedule_wrap(function()
        local pcb = self.pending[id]
        self.pending[id] = nil
        self:_clear_timer(id)
        if pcb then
          pcb({ code = -32099, message = ("request timed out after %dms"):format(tmo) }, nil)
        end
      end))
    end
  end
  -- (unchanged: empty-params dict, encode, write)
  ...
end
```

In **both** `Client:_on_close` and `Client:close`, cancel all armed timers before
flushing pending callbacks (otherwise a timer fires after close):

```lua
  for id in pairs(self.timers) do
    self:_clear_timer(id)
  end
```

### 1.3 `lua/tyo3/daemon.lua` — pass the config value into the client

In `new_client(root, st)`:

```lua
  return rpc.new({
    socket = st.socket,
    timeout_ms = config.get().request_timeout_ms,     -- ← add
    on_notification = ...,
    on_close = ...,
  })
```

### 1.4 Verify

Add to `tests/smoke.lua` (or a new `tests/rpc_timeout.lua`): connect a client whose
socket points at a daemon that is up, send a bogus method with a tiny override
`client:request("ping", {}, cb, { timeout_ms = 1 })` against a deliberately paused
actor — simplest is to assert the *shape*: a request with `timeout_ms = 1` to a real
but slow path resolves the callback with `err.code == -32099`. Keep it minimal:

```lua
-- after a connected client `c`:
local got
c:request("ping", {}, function(err) got = err end, { timeout_ms = 1 })
vim.wait(2000, function() return got ~= nil end, 10)
check("1ms timeout errors the callback", got and got.code == -32099, got and got.message)
```

Run:
```
devenv shell -- nvim --headless --clean \
  -u editors/tyo3.nvim/tests/minimal_init.lua \
  -c "luafile editors/tyo3.nvim/tests/smoke.lua"
```

---

## Phase 2 — Code-action UX (F.1–F.4)

**Goal:** the `gra` / tiny-code-action popup stops offering blind actions, titles them
with the entity, splits kinds for distinct icons, and adds a review-acknowledge
quickfix. All in `lua/tyo3/lsp.lua`, over verbs that already exist (`entity_at`,
`authored`, `author`).

### 2.1 Advertise kinds (and keep resolve off for now)

In `CAPS`:

```lua
  codeActionProvider = {
    codeActionKinds = { "refactor.rewrite", "quickfix", "source.tyo3" },
  },
```

Add a small kind-filter helper near the other conversion helpers:

```lua
-- LSP `context.only` is prefix-matched: a request for "refactor" matches
-- "refactor.rewrite". Return true if any tyo3 kind satisfies the filter.
local TYO3_KINDS = { "refactor.rewrite", "quickfix", "source.tyo3" }
local function has_tyo3_kind(only)
  for _, want in ipairs(only or {}) do
    for _, k in ipairs(TYO3_KINDS) do
      if k == want or k:sub(1, #want + 1) == want .. "." or want == k then
        return true
      end
    end
  end
  return false
end
```

### 2.2 Make the handler async + entity-gated, and enrich `ctx`

Replace `handlers["textDocument/codeAction"]` (note `_root` → `root`):

```lua
handlers["textDocument/codeAction"] = function(root, params, reply)
  local pctx = params.context or {}
  -- Respect an explicit kind filter (kind-scoped invocations / pickers).
  if pctx.only and not has_tyo3_kind(pctx.only) then
    return reply(nil, {})
  end
  -- Don't run daemon work for automatic (lightbulb / cursorhold) triggers.
  if pctx.triggerKind == 2 then
    return reply(nil, {})
  end

  local uri = params.textDocument.uri
  local start = (params.range and params.range.start) or { line = 0, character = 0 }
  local p = lsp_pos_to_daemon(start)

  -- Resolve the entity under the selection ONCE; providers key off ctx.entity.
  daemon_request(root, "entity_at", {
    path = uri_to_path(uri),
    line = p.line,
    col = p.col,
  }, function(_err, card)
    local ctx = {
      uri = uri,
      line = p.line,
      col = p.col,
      bufnr = vim.fn.bufnr(uri_to_path(uri)),
      root = root,
      entity = (card ~= nil and card ~= vim.NIL) and card or nil,
      diagnostics = pctx.diagnostics or {},
    }
    local actions = {}
    for _, provider in ipairs(M._code_action_providers) do
      local ok, contributed = pcall(provider, ctx)
      if ok and type(contributed) == "table" then
        vim.list_extend(actions, contributed)
      elseif not ok then
        vim.schedule(function()
          vim.notify("[tyo3] a code-action provider errored: " .. tostring(contributed), vim.log.levels.WARN)
        end)
      end
    end
    -- Deterministic order (stable across recordings): preferred first, then title.
    table.sort(actions, function(a, b)
      if (a.isPreferred and true) ~= (b.isPreferred and true) then
        return a.isPreferred and true or false
      end
      return (a.title or "") < (b.title or "")
    end)
    reply(nil, actions)
  end)
end
```

> The provider contract is unchanged for third parties — `register_code_action`
> providers that ignore the new `ctx.entity` / `ctx.diagnostics` keep working. Only
> *entity* providers should gate on `ctx.entity`.

### 2.3 Built-in explain/simplify provider — entity-gated + named title + kinds

Replace the built-in `M.register_code_action(function(ctx) ... end)` block:

```lua
M.register_code_action(function(ctx)
  if not ctx.entity then
    return {}                                   -- nothing under the cursor → no noise
  end
  local who = ctx.entity.qualified_name or ctx.entity.name or "entity"
  local function action(title, mode, kind)
    return {
      title = title,
      kind = kind,
      command = {
        title = title,
        command = "tyo3.explain",
        arguments = { { uri = ctx.uri, line = ctx.line, col = ctx.col, mode = mode, root = ctx.root } },
      },
    }
  end
  return {
    action(("tyo3: Explain `%s`"):format(who), "explain", "source.tyo3"),
    action(("tyo3: Simplify `%s`"):format(who), "simplify", "refactor.rewrite"),
  }
end)
```

### 2.4 Review-acknowledge quickfix provider + `tyo3.ack` command

Add a provider (place it after the explain provider) that fires only when the entity
carries a `needs_review` authored layer:

```lua
-- Offer an acknowledge quickfix when the entity under the cursor is flagged
-- needs_review on any authored layer. Re-authoring the note IS the engine's
-- acknowledge (it re-stamps the reviewed body hash), so this clears the WARN.
M.register_code_action(function(ctx)
  if not ctx.entity then
    return {}
  end
  local flagged = {}
  for layer, rec in pairs(ctx.entity.authored or {}) do
    if rec.status == "needs_review" then
      table.insert(flagged, layer)
    end
  end
  if #flagged == 0 then
    return {}
  end
  return {
    {
      title = "tyo3: Acknowledge review (re-author)",
      kind = "quickfix",
      isPreferred = true,
      command = {
        title = "tyo3: Acknowledge review",
        command = "tyo3.ack",
        arguments = { { uri = ctx.uri, root = ctx.root, did = ctx.entity.durable_id, layers = flagged } },
      },
    },
  }
end)
```

Register the command near the other `M.register_command` calls:

```lua
-- Acknowledge needs_review by re-authoring each flagged layer's CURRENT value
-- (re-read fresh at apply time, not the menu-time snapshot). Clears the WARN.
M.register_command("tyo3.ack", function(command, cmd_ctx)
  local args = (command.arguments or {})[1] or {}
  local bufnr = (cmd_ctx and cmd_ctx.bufnr) or vim.api.nvim_get_current_buf()
  local root = args.root or require("tyo3").root_for_buf(bufnr)
  if not root or not args.did then
    return
  end
  local layers = args.layers or {}
  local pending = #layers
  if pending == 0 then
    return
  end
  local function done_one()
    pending = pending - 1
    if pending > 0 then
      return
    end
    vim.schedule(function()
      if vim.api.nvim_buf_is_loaded(bufnr) then
        pcall(function() require("tyo3.decorate").apply(bufnr) end)
        if require("tyo3.config").layer_diagnostics_enabled() then
          M.refresh_layer_diagnostics(bufnr, root)
        end
      end
      vim.notify("[tyo3] review acknowledged", vim.log.levels.INFO)
    end)
  end
  for _, layer in ipairs(layers) do
    daemon_request(root, "authored", { layer = layer, durable_id = args.did }, function(err, av)
      if not err and av and av.value ~= nil and av.value ~= vim.NIL then
        daemon_request(root, "author", { layer = layer, durable_id = args.did, value = av.value }, function()
          done_one()
        end)
      else
        done_one()
      end
    end)
  end
end)
```

### 2.5 Update `executeCommandProvider`

```lua
  executeCommandProvider = { commands = { "tyo3.explain", "tyo3.run", "tyo3.ack" } },
```

### 2.6 Optional: a "thinking" beat in the explain command

In the existing `tyo3.explain` command, echo before the (potentially slow) call so the
popup → float gap isn't silent:

```lua
  vim.notify("[tyo3] explaining " .. (args.mode or "explain") .. " …", vim.log.levels.INFO)
  M.run_explain(bufnr, root, args, function(err, res) ... end)
```

(With Phase 5 the editor stays responsive during this; without it, at least the user
knows work is happening.)

### 2.7 Tests — extend `tests/lsp_codeaction.lua`

The existing on-entity assertions (`#actions == 2`, then `== 4`) **stay valid**: at
`checkout_pos` an entity resolves and is not `needs_review`, so the explain provider
yields 2 and the ack provider yields 0. Add three checks:

1. **Off-entity yields nothing.** Request codeAction at a blank/import line position and
   assert `#actions == 0`:
   ```lua
   local blank = { textDocument = { uri = store_uri },
     range = { start = { line = 0, character = 0 }, ["end"] = { line = 0, character = 0 } },
     context = { diagnostics = {} } }
   -- (line 0 char 0 is the module's first import — id_for returns nil there)
   local ca0 = vim.lsp.buf_request_sync(bufnr, "textDocument/codeAction", blank, 5000)
   local a0 = ca0 and ca0[client.id] and ca0[client.id].result
   check("no code action offered off-entity", type(a0) == "table" and #a0 == 0, a0 and #a0)
   ```
2. **Kinds are split.** Assert one action has `kind == "refactor.rewrite"` and one
   `kind == "source.tyo3"`.
3. **Ack appears + clears.** Author an `intent` note on `show_label`, edit its body via
   `sync_buffer` to flip it `needs_review`, request codeAction on it, assert an action
   with `command.command == "tyo3.ack"` exists and `isPreferred == true`; invoke
   `vim.lsp.commands["tyo3.ack"]({...})`, poll `review_state`, assert `show_label`
   no longer appears.

Run:
```
devenv shell -- nvim --headless --clean \
  -u editors/tyo3.nvim/tests/minimal_init.lua \
  -c "luafile editors/tyo3.nvim/tests/lsp_codeaction.lua"
```

### 2.8 README — code-action section

Update the Native LSP bridge table to add the code-action row and the ack quickfix, and
note kind filtering. (Also covered in Phase 4's README pass.)

---

## Phase 3 — Hero demo loop (A1/A2)

**Goal:** a short (~35s), looping, sidebar-forward GIF that leads with the money shot +
one LSP wow. Keep the existing tours as the "full" recordings.

### 3.1 New files

```
editors/tyo3.nvim/demo/hero/init.lua
editors/tyo3.nvim/demo/hero/hero.tape
```

`demo/hero/init.lua` — clone `demo/context/init.lua` (so the rich sidebar tracks the
cursor) but also enable the bridge for the `]d` beat:

```lua
-- (header + runtimepath resolution identical to demo/context/init.lua)
require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  auto_start = true,
  debounce_ms = 250,
  virtual_text = true,
  panel = "always",
  context = "cursor",          -- rich IDENTITY/NOTES/DOCS/SUMMARY/ACTIONS dock
  context_debounce_ms = 100,
  lsp = true,                  -- so ]d walks needs_review + type error
})
vim.schedule(function() require("tyo3.panel").open() end)
vim.opt.statusline = "  TyO3 ● live  %=%f  %l:%c "
```

`demo/hero/hero.tape` — three beats only, tight pacing (typing ~35ms, holds 1–2.5s):

```
Output editors/tyo3.nvim/demo/hero/hero.gif
Output editors/tyo3.nvim/demo/hero/hero.txt
Require nvim
Require python
Set Shell "bash"
Set FontSize 16
Set Width 1360
Set Height 840
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

# Pre-warm OFF camera: build project + open+reindex so the first on-camera sync is hot.
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Hide
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/hero/init.lua store.py" Enter
Sleep 6s
Type ":TyO3Reindex" Enter
Sleep 3s
Show

# Beat 1 — the money shot: note an entity, move it, the note rides the move.
Type "/def checkout" Enter
Sleep 1500ms
Type ":TyO3Note load-bearing checkout path" Enter
Sleep 1800ms
Type ":TyO3Move checkout checkout.py" Enter
Sleep 1800ms
Type ":e checkout.py" Enter
Sleep 2500ms

# Beat 2 — durable review + type error in ONE ]d stream (the novel part).
Type ":e store.py" Enter
Sleep 1200ms
Type "/def show_label" Enter
Sleep 600ms
Type ":TyO3Note keep this label stable" Enter
Sleep 1500ms
Type "/\.label" Enter
Type "lciwprice"
Escape
Sleep 600ms
Type ":w" Enter
Sleep 4s
Type "gg"
Sleep 300ms
Type "]d"
Sleep 1800ms
Type ":lua vim.diagnostic.open_float()" Enter
Sleep 2200ms
Escape

# Beat 3 — acknowledge the review from the code-action menu (Phase 2).
Type "]d"
Sleep 800ms
Type ":lua vim.lsp.buf.code_action()" Enter
Sleep 2200ms
Type "1" Enter
Sleep 2200ms

Type ":qa!" Enter
Sleep 1s
```

> `Hide`/`Show` keep the cold pre-warm out of frame. Beat 3 demos the new `tyo3.ack`
> quickfix and only works after Phase 2.

### 3.2 devenv script + README hero image

In `devenv.nix`, add a `demo-record-hero` script mirroring `demo-record` (around line
295):

```nix
  scripts.demo-record-hero.exec = ''
    echo "═══ Recording tyo3.nvim HERO demo (vhs) ═══"
    if ! command -v vhs >/dev/null 2>&1; then
      echo "vhs not found on PATH — is the devenv shell active?"; exit 1
    fi
    vhs editors/tyo3.nvim/demo/hero/hero.tape
  '';
```

In `editors/tyo3.nvim/README.md`, swap the hero image (line 8) to
`![tyo3.nvim](demo/hero/hero.gif)` and demote the full tour to a "full tour" link below.

### 3.3 Verify

```
devenv shell -- demo-record-hero
```
Check `demo/hero/hero.gif` is < ~800 KB and the run is ~30–40s. Trim sleeps if heavier.

---

## Phase 4 — Cleanups + README accuracy (B3, B4, C)

### 4.1 B3 — delete the dead renderer

`lua/tyo3/card.lua`: remove `M.context_lines` (the whole function, ~lines 84–144) and
fix the module header — drop the false "and the panel's CONTEXT section (compact)" claim.
The header should read: the card formatter for the `:TyO3Inspect` float (`build_lines`).
Grep to confirm no caller remains:

```
grep -rn "context_lines" editors/tyo3.nvim/   # expect: no matches
```

### 4.2 B4 — reclaim buffer-keyed state

Add a cleanup function in `lua/tyo3/init.lua`:

```lua
--- Drop per-buffer caches/timers when a buffer is wiped (bufnrs get reused).
function M.on_buf_cleanup(bufnr)
  local t = M._debounce[bufnr]
  if t then pcall(function() t:stop(); t:close() end) end
  M._debounce[bufnr] = nil
  M._root_by_buf[bufnr] = nil
  M._last_synced[bufnr] = nil
  require("tyo3.context").forget(bufnr)
end
```

Add `forget` to `lua/tyo3/context.lua`:

```lua
function M.forget(bufnr)
  local t = M._debounce[bufnr]
  if t then pcall(function() t:stop(); t:close() end) end
  M._debounce[bufnr] = nil
  M._last_key[bufnr] = nil
end
```

Wire an autocmd in `plugin/tyo3.lua` (in the `tyo3` augroup):

```lua
vim.api.nvim_create_autocmd({ "BufWipeout", "BufDelete" }, {
  group = group,
  pattern = "*.py",
  callback = function(ev)
    require("tyo3").on_buf_cleanup(ev.buf)
  end,
})
```

(`decorate.name_cache` is keyed by durable_id, not bufnr, and is intentionally
last-seen; leave it.)

### 4.3 C — README accuracy

In `editors/tyo3.nvim/README.md`:
- **Requirements:** split the version floor:
  > - Neovim ≥ 0.10 for the bespoke UI (`vim.uv`).
  > - **Neovim 0.12** for the native LSP bridge (`setup{ lsp = true }`) — it uses the
  >   in-process `vim.lsp` server contract, type-hierarchy capability keys, and client
  >   command resolution verified against 0.12.
- **Protocol → Notifications table:** add the `derived` row
  (`{layer, durable_id, status, artifact, revision}`-ish — match `bus_pump._emit_derived`).
- **Protocol → Requests table:** add `layers`, `layer_ids`, `diagnostics_at`,
  `context_pack`, `explain`, and `subscribe`.
- **Native LSP bridge table:** add the code-action row
  (`textDocument/codeAction` → `entity_at` + client commands `tyo3.explain` / `tyo3.run`
  / `tyo3.ack`), and move code actions out of the "Not yet bridged" list.

### 4.4 Verify

```
devenv shell -- nvim --headless --clean \
  -u editors/tyo3.nvim/tests/minimal_init.lua \
  -c "luafile editors/tyo3.nvim/tests/smoke.lua"
devenv shell -- nvim --headless --clean \
  -u editors/tyo3.nvim/tests/minimal_init.lua \
  -c "luafile editors/tyo3.nvim/tests/context.lua"
```

---

## Phase 5 — Responsive `explain` (B1)

**Goal:** a slow (real-API) `explain` must not freeze the editor. Two coordinated
changes are required, because the editor uses **one** connection whose reader thread
dispatches requests **serially**:

1. **Server:** dispatch each request on a small worker pool so one slow request doesn't
   serialize the connection.
2. **Handler:** run the LLM call *outside* `actor.submit`, so the actor (the shared
   serialization point) isn't held during the network round-trip.

Neither alone suffices: with only (2) the connection's reader thread still blocks on the
slow handler; with only (1) the slow handler still holds the actor and starves every
other request's `actor.submit`.

### 5.1 Server — concurrent dispatch (`src/tyo3/daemon/server.py`)

Add a bounded pool to `DaemonServer.__init__`:

```python
from concurrent.futures import ThreadPoolExecutor
...
        self._dispatch_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tyo3-rpc")
```

Keep the reader loop reading serially (preserves read order) but offload the
**dispatch+reply** to the pool. Refactor `_handle_line` so parsing + `subscribe`
(per-connection state, cheap) stay inline, and the `Handlers.dispatch` path runs on the
pool:

```python
    def _handle_line(self, client: _Client, line: str) -> None:
        try:
            req = parse_request(line)
        except ProtocolError as e:
            client.send(encode_error(e.request_id, e.code, e.message))
            return
        if req.is_notification:
            return
        if req.method == "subscribe":
            ...  # unchanged, handled inline
            return
        # Offload the (possibly slow) handler so a slow request — e.g. `explain`
        # hitting a real LLM — doesn't serialize this connection's other requests.
        # Responses are id-matched on the client, so out-of-order replies are fine.
        self._dispatch_pool.submit(self._dispatch_and_reply, client, req)

    def _dispatch_and_reply(self, client: "_Client", req) -> None:
        try:
            result = self._handlers.dispatch(req.method, req.params)
            client.send(encode_response(req.id, result))
        except ProtocolError as e:
            client.send(encode_error(req.id, e.code, e.message))
        except Exception as e:  # noqa: BLE001
            log.exception("handler error for method %s", req.method)
            client.send(encode_error(req.id, ENGINE_ERROR, f"{type(e).__name__}: {e}", data={"method": req.method}))
```

Shut the pool down in `DaemonServer.shutdown()` (after the accept loop stops, before/with
the actor teardown):

```python
        self._dispatch_pool.shutdown(wait=False, cancel_futures=True)
```

**Safety notes**
- `client.send` is already guarded by `_send_lock` — concurrent replies are serialized.
- `Handlers` is stateless per request (only reads `self._tracker`/`self._actor`); the
  actor remains the single mutation/serialization point, so revision ordering is
  unaffected by which pool thread submits.
- `max_workers=8` bounds thread growth under a request burst.

### 5.2 Handler — LLM off the actor (`src/tyo3/daemon/handlers.py`)

Split `explain` into three steps: gather (actor) → LLM (off actor) → author (actor):

```python
    def explain(self, params: dict[str, Any]) -> dict[str, Any] | None:
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        mode = self._explain_mode(params)
        rel = self._relpath(path)

        # 1) Gather context on the actor (cheap, snapshot reads).
        def gather(s: TyO3Session) -> dict[str, Any] | None:
            did = s.id_for(rel, line, col)
            if did is None:
                return None
            return {"did": did, "ctx": self._gather_context(s, rel, line, col, did, mode=mode)}

        prep = self._actor.submit(gather)
        if prep is None:
            return None
        did, ctx = prep["did"], prep["ctx"]

        # 2) Run the LLM OFF the actor — the network round-trip holds no session lock.
        text = _llm(_build_explain_prompt(ctx, mode), system=_EXPLAIN_SYSTEM[mode])

        # 3) Author the result back on the actor (another cheap hop).
        def store(s: TyO3Session) -> None:
            s.author("explain", did, {
                "text": text, "mode": mode, "model": llm_model(), "generated_at": _utcnow_iso(),
            })

        self._actor.submit(store)
        return {"durable_id": did, "text": text, "mode": mode}
```

> Trade-off: `id_for` + author are now two actor hops instead of one, and a racing edit
> between them could move the entity. That's benign — the explain is keyed by durable id,
> so it still lands on the right entity; worst case the stamped reviewed-hash reflects the
> body at author time (already the documented behavior).

### 5.3 Lua side

With Phase 1's per-request timeout, give `explain`/`check` a longer ceiling so a real LLM
isn't cut off. In `lua/tyo3/lsp.lua` `M.run_explain` → `M.run_verb`, pass an override
through `daemon_request`. Extend `daemon_request` to accept opts:

```lua
local function daemon_request(root, method, params, cb, opts)
  daemon.ensure(root, function(err, client)
    if err then cb(err, nil); return end
    client:request(method, params, function(rerr, res)
      if res == vim.NIL then res = nil end
      cb(rerr, res)
    end, opts)
  end)
end
```

and for the explain/run paths pass `{ timeout_ms = 60000 }`.

### 5.4 Tests

- `src/tyo3/daemon/tests/test_handlers.py`: keep the existing `explain` assertion (still
  returns `{durable_id, text, mode}`; offline stub is instant). Add a test that two
  requests on one connection are **not** serialized: open a connection, fire a slow verb
  (monkeypatch `_llm` to sleep), then a `ping`, and assert `ping` returns before the slow
  verb completes (timestamps). This proves Phase 5.1.
- Run:
  ```
  devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
  ```
- Re-run `tests/lsp_codeaction.lua` (explain end-to-end) to confirm no regression.

---

## Phase 6 — Resolvable Simplify → diff preview (F.5)

**Goal:** tiny-code-action's preview pane shows a real before/after diff for "Simplify",
and Enter applies it as an edit (so the durable note rides the rewrite). This is the one
item needing a daemon verb that returns an **edit**, plus LSP `codeAction/resolve`.

### 6.1 Engine/daemon — a rewrite verb

Add a handler `simplify_edit` (name TBD) that returns an LSP-shaped `WorkspaceEdit`
replacing the entity body with the LLM's rewrite, rather than prose. It reuses the
explain context-gather + the off-actor LLM split from Phase 5, but:
- prompts the model to return *only* the rewritten entity source, and
- maps the entity's current range → a single `TextEdit` (`{range, new_text}`) per file.

Shape (mirrors `rename`'s `changes`):

```python
def simplify_edit(self, params) -> dict[str, Any] | None:
    # gather (actor) → LLM (off actor) → build edit from the entity's range
    # returns {"durable_id": did, "changes": { "<relpath>": [ { "range": ..., "new_text": ... } ] } }
```

> The model output must be constrained to a parseable replacement. Keep a guard: if the
> rewrite fails to parse (reuse the engine's tree-sitter parse), return `null` so the
> action degrades to the prose float.

### 6.2 LSP — advertise resolve + lazy edit

In `CAPS`:

```lua
  codeActionProvider = {
    resolveProvider = true,
    codeActionKinds = { "refactor.rewrite", "quickfix", "source.tyo3" },
  },
```

Change the Simplify action to carry `data` and **no** `edit` initially:

```lua
{
  title = ("tyo3: Simplify `%s`"):format(who),
  kind = "refactor.rewrite",
  data = { uri = ctx.uri, line = ctx.line, col = ctx.col, root = ctx.root, kind = "simplify" },
}
```

Add the resolve handler:

```lua
handlers["codeAction/resolve"] = function(root, action, reply)
  local d = action.data or {}
  if d.kind ~= "simplify" then
    return reply(nil, action)         -- nothing to resolve
  end
  daemon_request(d.root or root, "simplify_edit", {
    path = uri_to_path(d.uri), line = d.line, col = d.col,
  }, function(err, res)
    if err or not res or not res.changes then
      return reply(nil, action)       -- degrade: no edit → no preview, still selectable
    end
    local changes = {}
    for relpath, edits in pairs(res.changes) do
      local uri = path_to_uri(d.root or root, relpath)
      local te = {}
      for _, e in ipairs(edits) do
        table.insert(te, { range = daemon_range_to_lsp(e.range), newText = e.new_text })
      end
      changes[uri] = te
    end
    action.edit = { changes = changes }
    reply(nil, action)
  end, { timeout_ms = 60000 })
end
```

Wire `codeAction/resolve` into the `_server` request switch (it already routes by
`handlers[method]`, so just registering the handler is enough).

### 6.3 Tests + demo

- Add a `tests/lsp_codeaction.lua` check: request Simplify, then
  `codeAction/resolve` it, assert the resolved action carries `edit.changes` with a
  `newText` for the entity's file.
- Add a hero/full-tour beat: cursor on a function → `gra` → focus "Simplify" → preview
  pane shows the diff → Enter → buffer updated, note still attached.

---

## Phase 7 — Polish (B5, D)

### 7.1 B5 — debounce + cancel push diagnostics

In `lua/tyo3/lsp.lua`, wrap `M.publish_diagnostics` with a per-root debounce and drop the
result of a superseded `check`. Sketch:

```lua
M._diag_debounce = M._diag_debounce or {}   -- root -> uv timer
M._diag_gen = M._diag_gen or {}             -- root -> monotonically increasing gen

function M.publish_diagnostics(root, relpaths)
  local dispatchers = M._dispatchers_by_root[root]
  if not dispatchers or not dispatchers.notification then return end
  local uv = vim.uv or vim.loop
  local t = M._diag_debounce[root]
  if t then pcall(function() t:stop(); t:close() end) end
  local timer = uv.new_timer()
  M._diag_debounce[root] = timer
  timer:start(150, 0, vim.schedule_wrap(function()
    timer:stop(); timer:close(); M._diag_debounce[root] = nil
    local gen = (M._diag_gen[root] or 0) + 1
    M._diag_gen[root] = gen
    for _, path in ipairs(relpaths or {}) do
      daemon_request(root, "check", { path = path }, function(err, res)
        if err or M._diag_gen[root] ~= gen then return end   -- superseded → drop
        dispatchers.notification("textDocument/publishDiagnostics", {
          uri = path_to_uri(root, path),
          diagnostics = daemon_diags_to_lsp(res and res.diagnostics),
        })
      end)
    end
  end))
end
```

### 7.2 D — minor fixes

- **`panel.on_refinement`** (`lua/tyo3/panel.lua`): guard against double-annotating a
  revision line — track a per-revision "narrowed" flag and skip if already appended.
- **`telescope.jump_to`** (`lua/tyo3/telescope.lua`): prefer a precise jump. `locate`
  returns `file::qualified_name`; if the picker entry already carries a range (the
  affected/entities entries come from the decorate cache, which has ranges), jump to it
  directly instead of `vim.fn.search`. Fall back to search only when no range is known.
- **`docs.index` numeric-selection note** (`demo/context/context.tape`): add a comment
  that the `"1"`/`"3"` selection assumes the default `vim.ui.select`.

### 7.3 Verify (full pass)

```
devenv shell -- build
devenv shell -- test-fast
devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
for t in smoke context lsp lsp_nav lsp_codeaction review_dedup; do
  devenv shell -- nvim --headless --clean \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/$t.lua"
done
devenv shell -- demo-record-hero
```

---

## Cross-cutting checklist

- [ ] Phase 1: rpc timeout + config default + daemon wiring + smoke check
- [ ] Phase 2: async entity-gated codeAction, named titles, split kinds, `tyo3.ack`,
      `codeActionKinds`, command list, codeaction test extensions
- [ ] Phase 3: `demo/hero/{init.lua,hero.tape}`, devenv script, README hero swap
- [ ] Phase 4: delete `card.context_lines` + header, buffer-cleanup autocmd, README
      accuracy (version floor, protocol tables, codeaction row)
- [ ] Phase 5: server dispatch pool + shutdown, off-actor `explain`, longer explain
      timeout, non-serialization pytest
- [ ] Phase 6: `simplify_edit` verb, `resolveProvider`, `codeAction/resolve` handler,
      resolve test, demo beat
- [ ] Phase 7: push-diag debounce/cancel, refinement dedup, telescope precise jump

## Notes / decisions to confirm before starting

1. **`request_timeout_ms` default (Phase 1).** 20s is safe post-connect (cold indexing
   happens before the socket binds). Confirm no normal verb legitimately exceeds it
   besides explain/check (which get overrides).
2. **Server concurrency (Phase 5).** A bounded `ThreadPoolExecutor(8)` is the minimal
   change. If you'd rather keep the daemon strictly single-threaded, the alternative is
   making `explain` itself async (return an ack, deliver via notification) — but that
   changes the popup→float UX and is more invasive. Recommend the pool.
3. **Simplify rewrite (Phase 6)** depends on a model that returns parseable replacement
   source; gate on a parse check and degrade to prose on failure.
