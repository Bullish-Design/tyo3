# Project 32 — implementation guide

Ordered build steps for the scope in [KICKOFF.md](KICKOFF.md). Each step names
its files, its tests, and the command that proves it. Complete a step, verify
it, and land it before starting the next one.

Route all version control through `gitman`. Run every command inside
`devenv shell` with a `SECRETSPEC_REASON`.

## Conventions used below

- Paths are repository-relative.
- Sketches show intent, not final text. Match the surrounding code's style.
- "Verify" is the command that must pass before the step lands.

---

## Step 0 — Build-profile guard

**Why first.** `devenv shell -- build` (debug) and `devenv shell -- build-release`
write the same file, `src/tyo3/_native_impl.abi3.so`. Debug runs the commit path
about 6x slower. Nothing reports which profile is loaded. Every measurement in
this project is void without this guard.

### 0.1 Export the profile from Rust

`rust/src/lib.rs`, in the `native_impl` module initialiser, beside the existing
`m.add(...)` calls:

```rust
// The Cargo profile this extension was built with. `maturin develop` and
// `maturin develop --release` install to the SAME path, so the profile is
// otherwise invisible at runtime. Benchmarks assert on this.
m.add(
    "__build_profile__",
    if cfg!(debug_assertions) { "debug" } else { "release" },
)?;
```

### 0.2 Surface it in Python

`src/tyo3/__init__.py`, next to the existing `_HAS_NATIVE` block:

```python
def build_profile() -> str:
    """The Cargo profile of the loaded native extension: "debug", "release",
    or "unknown" when the extension is absent.

    Debug builds leave the local ``tyo3`` crate at ``opt-level = 0`` and run the
    commit path roughly 6x slower. Never quote a performance number taken from
    one.
    """
    try:
        from tyo3._native_impl import __build_profile__

        return __build_profile__
    except ImportError:
        return "unknown"


def require_release_build() -> None:
    """Raise unless the loaded extension is a release build.

    Call this at the top of any benchmark or performance assertion.
    """
    profile = build_profile()
    if profile != "release":
        raise RuntimeError(
            f"this measurement needs a release build; loaded profile is "
            f"'{profile}'. Run: devenv shell -- build-release"
        )
```

Add both names to `__all__`.

### 0.3 Report it in the dev scripts

`devenv.nix`, in `scripts.status.exec` and `scripts.check-so.exec`, print the
profile alongside the file listing:

```bash
PYTHONPATH="$DEVENV_ROOT/src" python -c \
  "import tyo3; print('    profile:', tyo3.build_profile())"
```

### 0.4 Tests

`tests/test_build_profile.py`:

- `build_profile()` returns one of `debug`, `release`, `unknown`.
- When it returns `debug`, `require_release_build()` raises `RuntimeError`.
- When it returns `release`, `require_release_build()` returns `None`.

### 0.5 Keep the benchmarks

Add `tests/benchmarks/test_commit_cost.py`, marked `@pytest.mark.benchmark` so
the default suite skips it (`pyproject.toml` already excludes that marker). It
calls `require_release_build()` first, then reproduces the KICKOFF's evidence
table. This makes the numbers re-checkable instead of lore.

**Verify**

```text
SECRETSPEC_REASON="Project 32 step 0 release build" devenv shell -- build-release
SECRETSPEC_REASON="Project 32 step 0 tests" devenv shell -- pytest tests/test_build_profile.py
```

---

## Step 1 — Protocol corrections

Four additive changes. None alters an existing field's meaning, so the Neovim
client keeps working. Confirm that by running the Neovim suite at the end.

### 1.1 Typed error data

`src/tyo3/daemon/protocol.py` — give `ProtocolError` a `data` payload:

```python
def __init__(
    self,
    message: str,
    *,
    code: int = INVALID_REQUEST,
    request_id: Any = None,
    data: Any = None,
) -> None:
    ...
    self.data = data
```

Pass `e.data` through both `encode_error` call sites in
`src/tyo3/daemon/server.py`.

In `DaemonServer._dispatch_and_reply`, add the exception type to the engine
error payload so the client never parses prose:

```python
except Exception as e:
    log.exception("handler error for method %s", req.method)
    client.send(
        encode_error(
            req.id,
            ENGINE_ERROR,
            f"{type(e).__name__}: {e}",
            data={"method": req.method, "error_type": type(e).__name__},
        )
    )
```

### 1.2 Instance identity

`src/tyo3/daemon/handlers.py`, in `Handlers.__init__`:

```python
# `session_id` is sha1(root) and is STABLE across daemon restarts, so a client
# cannot use it to detect that the revision counter reset. `instance_id` is
# fresh per daemon process and is the restart signal.
self._instance_id = uuid.uuid4().hex
```

Return `instance_id` and `protocol_version: 1` from both `ping` and `open`.

### 1.3 Revision-bearing reads

Session reads resolve against a cached head snapshot, not a live handle
(`src/tyo3/session/session.py`, `_native`). Report that snapshot's revision,
not `session.head`.

Add to `TyO3Session`:

```python
@property
def read_revision(self) -> int:
    """The revision that this session's reads currently observe.

    Reads resolve against a pinned head snapshot that a mutation invalidates,
    so this is the honest "revision observed for this result" — use it instead
    of ``head`` when stamping a read response.
    """
    self._check_open()
    return self._native().revision
```

Then stamp these handlers: `context_pack`, `entity_at`, `symbols`,
`definition`, `references`, `hover`, `check`, `diagnostics_at`, and the new
`impact`.

- Handlers that already open a snapshot (`entity_at`, `symbols`) use
  `snap.revision`.
- The rest use `s.read_revision` inside the same actor work item.
- `hover` returns a bare model dump and can be `null`. Inject `revision` into
  the dump when it is not null. Pydantic ignores extra keys by default, so
  re-validation still works; add a test that asserts it.

### 1.4 Symbol truncation

`symbols` caps at `_MAX_SYMBOLS = 500` with a `break` **before** the sort, so an
over-cap query returns an arbitrary subset in graph order that looks complete.
Return the fact:

```python
return {"symbols": out, "truncated": truncated, "limit": _MAX_SYMBOLS}
```

### 1.5 Tests

Extend `tests/daemon/test_handlers.py`:

- `ping` and `open` both carry `protocol_version` and `instance_id`.
- Two `Handlers` instances produce different `instance_id` values.
- Each stamped read returns an integer `revision` and keeps every field the
  existing tests assert.
- A `hover` payload still validates as `HoverResult` after stamping.
- `symbols` sets `truncated` when the cap is hit, and clears it when not.

Extend `tests/daemon/test_end_to_end.py`:

- An engine error arrives with `data.error_type` set.

**Verify**

```text
SECRETSPEC_REASON="Project 32 step 1 daemon tests" devenv shell -- pytest tests/daemon
SECRETSPEC_REASON="Project 32 step 1 nvim"         devenv shell -- test-nvim
```

---

## Step 2 — The `impact` route

The one new handler, and the reason an agent connects to TyO3 at all.

### 2.1 Handler

`src/tyo3/daemon/handlers.py`. `CodeGraph.transitive_dependents(durable_id)`
returns a `set[str]` using `rx.ancestors` over a cached semantic subgraph, so
repeated calls against one snapshot are cheap after the first.

Accept **either** a position or a durable id. Position keys go stale the moment
the agent edits; durable ids do not.

```python
def impact(self, params: dict[str, Any]) -> dict[str, Any] | None:
    """What breaks if the entity at this target changes.

    Returns the entity's transitive dependents (semantic edges only), grouped
    by file. Accepts a position *or* a ``durable_id`` — after an edit, the
    agent's cached positions are stale but its ids are not.

    ``null`` when nothing resolves at the target.
    """
    durable_id = params.get("durable_id")
    if durable_id is None:
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        rel = self._relpath(path)
    elif not isinstance(durable_id, str):
        raise ProtocolError("'durable_id' must be a string", code=INVALID_PARAMS)

    def work(s: TyO3Session) -> dict[str, Any] | None:
        did = durable_id if durable_id is not None else s.id_for(rel, line, col)
        if did is None:
            return None
        with s.snapshot() as snap:
            g = snap.graph()
            if _node_by_id(g, did) is None:
                return None
            dependent_ids = sorted(g.transitive_dependents(did))
            truncated = len(dependent_ids) > _MAX_IMPACT
            items = []
            for dep_id in dependent_ids[:_MAX_IMPACT]:
                node = _node_by_id(g, dep_id)
                if node is None or not is_entity_node(dep_id, external=node.external):
                    continue
                items.append({
                    "durable_id": dep_id,
                    "name": node.name,
                    "qualified_name": node.qualified_name,
                    "kind": node.kind.value,
                    "path": node.file,
                    "range": _range_dict(node.range),
                })
            items.sort(key=lambda d: (d["path"], d["range"]["start"]["line"]))
            return {
                "durable_id": did,
                "dependents": items,
                "count": len(items),
                "truncated": truncated,
                "limit": _MAX_IMPACT,
                "revision": snap.revision,
            }

    return self._actor.submit(work)
```

Add `_MAX_IMPACT = 500` beside `_MAX_SYMBOLS`, and register `"impact"` in
`_METHODS`.

### 2.2 Tests

`tests/daemon/test_handlers.py`:

- `impact` is registered and reachable.
- A known shop-project entity returns its callers as dependents.
- A leaf entity returns an empty list, not `null`.
- An off-entity position returns `null`.
- Resolving by `durable_id` and by position yields the same result.
- The response carries `revision`.

**Verify**

```text
SECRETSPEC_REASON="Project 32 step 2 daemon tests" devenv shell -- pytest tests/daemon
```

---

## Step 3 — `AgentClient`

A synchronous client over the existing socket. All logic lives here. The CLI and
any future MCP adapter are thin shells over it.

### 3.1 Module

New package `src/tyo3/agent/`:

- `__init__.py` — exports `AgentClient` and the error types.
- `client.py` — the client.
- `errors.py` — typed errors.

```python
class AgentError(Exception):
    """Base for every client-side failure."""

class DaemonUnavailable(AgentError):
    """The daemon is not running and could not be started."""

class EngineError(AgentError):
    """The daemon raised. Carries ``error_type`` from the wire."""
    def __init__(self, message: str, *, error_type: str, method: str) -> None: ...

class RevisionEvicted(EngineError):
    """The requested revision is no longer retained (256-revision window)."""

class SessionClosed(EngineError):
    """The daemon closed the project."""

class RequestTimeout(AgentError):
    """The result is UNKNOWN. It is not a cancellation.

    The daemon may have committed. Reconcile with ``status`` and ``changed``
    before retrying.
    """
```

Map `data.error_type` to `RevisionEvicted` / `SessionClosed`, and fall back to
`EngineError`. That mapping is why Step 1.1 exists.

### 3.2 Responsibilities

- Resolve the socket with `daemon.server.default_socket_path(root)`.
- Connect; on failure spawn `tyo3-daemon --root <root> --print-socket`, wait for
  the socket line, then connect. Allow at least 60 s: `open` costs ~1.7 s on
  this repository and scales with project size.
- Treat exit code 3 (`DaemonAlreadyRunning`) as success and connect to the
  running daemon.
- Frame newline-delimited JSON-RPC, correlate by request id, drop
  notifications on a queue the caller may ignore. `tests/daemon/test_end_to_end.py`
  already contains a working reader-thread client — follow its shape.
- Verify `open().root` matches the requested root. The `open` handler ignores
  its `root` parameter, so a mismatch means the daemon serves a different
  project.
- Expose one method per CLI verb, returning plain dicts.
- Record `instance_id` at connect. When it changes, raise or flag: every cached
  revision and position is void.

### 3.3 Tests

`tests/daemon/test_agent_client.py`, reusing the `shop_project` fixture:

- Starts a daemon with no Neovim and reaches `status`.
- Autostart is idempotent: a second client joins the same daemon.
- `status` reports root, session id, instance id, revision.
- An engine failure raises the typed error, not a string match.
- A timeout raises `RequestTimeout`, and a follow-up `status` shows whether the
  work landed — documenting "unknown", not "cancelled".
- Disconnect, reconnect, re-read: converges with no notification replay.
- **The contract test:** write a file with plain `pathlib`, call `sync`, and
  assert the new entity appears — one revision, precise delta. Then assert no
  daemon call wrote a source file.

**Verify**

```text
SECRETSPEC_REASON="Project 32 step 3 client tests" devenv shell -- pytest tests/daemon
```

---

## Step 4 — The `tyo3-agent` CLI

### 4.1 Dependency

Add `typer-slim` to a `cli` optional-dependency group in `pyproject.toml`, and
register the script:

```toml
[project.scripts]
tyo3-agent = "tyo3.agent.cli:app"
```

Use `typer-slim`, not `typer`: the full package pulls `rich` and `shellingham`
into a library whose entire runtime dependency list is `pydantic` and
`rustworkx`. Confirm the slim package name against the current release before
committing. This is a deliberate departure from the repository's argparse
convention, justified because this CLI is a product surface rather than a dev
entry point.

### 4.2 Shape

`src/tyo3/agent/cli.py`:

- One Typer command per verb from the KICKOFF table.
- A shared `--root` option, defaulting to the discovered project root.
- `TARGET` accepts `path:line:col` **or** `--id <durable_id>`.
- Output: JSON on stdout when `not sys.stdout.isatty()`, or when `--json` is
  passed. Human table otherwise. Diagnostics to stderr, always.
- Exit codes: 0 success, 1 engine error, 2 usage error, 3 daemon unavailable,
  4 conflict or stale revision.
- No engine logic. Every command parses arguments, calls one `AgentClient`
  method, and renders.

### 4.3 Tests

`tests/daemon/test_agent_cli.py`:

- Each verb runs end to end against a real daemon.
- Piped stdout emits parseable JSON with no ANSI codes.
- A usage error exits 2; an unreachable daemon exits 3.
- `--help` lists every verb.

**Verify**

```text
SECRETSPEC_REASON="Project 32 step 4 cli tests" devenv shell -- pytest tests/daemon
```

---

## Step 5 — Documentation

- `README.md`: an "Agent use" section with the loop, the three contract rules,
  and the measured per-iteration cost.
- `CONTRIBUTING.md`: extend the existing "Stale `.so` gotcha" with the profile
  gotcha — both profiles install to the same path, and `tyo3.build_profile()`
  tells you which one is loaded.
- `src/tyo3/agent/__init__.py`: a module docstring stating the contract.

State plainly that `sync_buffer` and `sync_buffers` are the Neovim
unsaved-buffer path and that agents must not call them.

---

## Step 6 — Full verification and landing

```text
SECRETSPEC_REASON="Project 32 release build"  devenv shell -- build-release
SECRETSPEC_REASON="Project 32 check-rust"     devenv shell -- check-rust
SECRETSPEC_REASON="Project 32 clippy"         devenv shell -- clippy
SECRETSPEC_REASON="Project 32 parity oracle"  devenv shell -- parity-oracle
SECRETSPEC_REASON="Project 32 tests"          devenv shell -- tests
SECRETSPEC_REASON="Project 32 nvim"           devenv shell -- test-nvim
SECRETSPEC_REASON="Project 32 pre-save"       devenv shell -- gitman status
```

Land each step as its own lane. Do not mix unrelated changes.

Filter ambient `mypi` / `MYPI` setup noise from captured evidence.

---

## Risks

- **Typer dependency.** The repository uses argparse everywhere and keeps two
  runtime dependencies. If `typer-slim` proves heavier than expected, fall back
  to argparse. The `AgentClient` layering makes that swap cheap, which is the
  main reason for the layering.
- **`impact` cost is unmeasured.** `_semantic_subgraph` builds and caches a
  filtered graph on first use. Measure `impact` on this repository during
  Step 2 and record the number in the KICKOFF evidence table.
- **Hover stamping.** Injecting `revision` into a bare model dump is the one
  change that touches an LSP-shaped payload. The Neovim suite is the gate.
- **Identity registry growth.** Entities that appear and disappear leave
  orphaned records that corrupt delta counts on later runs. Surface `gc` and
  `notes --stale` so an agent can observe and clear this. Reset
  `.tyo3/identity.db` before any benchmark run.
