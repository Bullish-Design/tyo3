# Project 32 follow-up — implementation kickoff

**Status: scope fixed from measurement. Implementation not started.**

Paste this document (or point a session at it) to start the fixes.

Read [FOLLOWUP-INVESTIGATION.md](FOLLOWUP-INVESTIGATION.md) first. It holds the
measured evidence, the finding numbers (F1…F14), and the behaviour matrix.
This document holds the **chosen fix** for each finding and the order to build
them.

---

## Context

Repository: `/home/andrew/Documents/Projects/tyo3`

Project 32 shipped a headless agent plane: `AgentClient`, the `tyo3-agent`
CLI, the `impact` route, four protocol corrections, and a build-profile guard.
A measured follow-up investigation then found three P0 defects and one large
performance defect. This work fixes them.

### Non-negotiable product contract

1. The agent owns the filesystem.
2. TyO3 never writes source files.
3. The agent never sends overlay text. `sync_buffer` / `sync_buffers` are
   exclusively the Neovim unsaved-buffer path. Headless agents edit files
   themselves and call `sync` (reindex) afterwards.

Do not weaken, reinterpret, or work around these three rules. Do not add a
daemon filesystem-write route. Do not add Neovim overlay behaviour to the
agent plane.

### Authority

- `KICKOFF.md` — the original Project 32 scope. Still authoritative for it.
- `IMPLEMENTATION.md` — authoritative for the work already completed.
- `FOLLOWUP-INVESTIGATION.md` — **authoritative for this work.**
- `INVESTIGATION.md` — predates measurement. Not a roadmap. Ignore it.

---

## Operational rules

- Run every command inside `devenv shell` with a `SECRETSPEC_REASON`.
- Use `build` for release builds. Use `build-debug` only for debug builds.
- Never quote a performance number from a debug build. Call
  `tyo3.require_release_build()` at the top of any benchmark.
- Delete `.tyo3/identity.db` before any benchmark run. Stale identity records
  resurrect and corrupt delta counts.
- Filter ambient `mypi` / `MYPI` setup noise out of captured evidence.
- Route **all** version control through `gitman`. Never call `git` or `jj`
  directly.
- Run `test-nvim` after **any** change to a daemon response shape, to the
  socket path derivation, or to the root lock. Steps 1, 3, and 5 all qualify.
- Use the repository's `repoman` and build/investigation-loop guidance.
- One lane per step. Verify before landing. Do not mix unrelated changes.
- Preserve the existing user lanes `038-devman-consumer-tyo3` and
  `039-devman-item3-tyo3`, and any other unrelated worktree changes.

---

## The fixes, in build order

Steps 1–4 are P0. Step 5 is the large performance win. Steps 6–8 finish the
surface. Each step names the finding it closes, the chosen fix, the reason
that fix beats the alternatives, and its verification gate.

---

### Step 1 — One daemon per root, for real (closes **F2**, **F13**; folds in P2-3)

**Do this first.** It is the only defect that can damage state.

**The defect.** `server.py:133` derives the `flock` path from the *socket*
path, not the root. `AgentClient` derives the socket as `sha1(resolved_root)`
(`server.py:57`); `tyo3.nvim` passes `--socket` derived as `sha256(root)`
(`daemon.lua:41-44`). Different paths, different locks, so two daemons run on
one root with two writable sessions over one `.tyo3/` identity registry.
Measured directly: both `_acquire_lock()` calls succeed.

**The fix — three parts. All three are required.**

1. **Key the lock on the root, at a path in the root.** Move the lock to
   `<root>/.tyo3/daemon.lock`. Create `<root>/.tyo3/` in `_acquire_lock`
   before taking the lock — opening the session is what writes the sidecar,
   and the lock must still precede it (`server.py:212-214`).

   *Why there, not in `$XDG_RUNTIME_DIR`:* the lock must be the same
   filesystem object for every process that opens the root, whatever socket
   path or user it chose. A runtime-dir lock is per-user, so two users would
   still get two daemons over one sidecar. `.tyo3/` is the thing being
   protected and is already gitignored. Creating it is a sidecar write, not a
   source write — contract rule 2 is untouched.

2. **Unify the socket derivation on `sha256`.** Change
   `default_socket_path` (`server.py:56-57`) from `sha1` to `sha256`, keeping
   `Path(root).resolve()`.

   *Why this direction:* Neovim's only builtin digest is `vim.fn.sha256`.
   Moving Lua to `sha1` would mean hand-rolling a hash in Lua. Moving Python
   to `sha256` is a one-line change. It orphans any running daemon's socket,
   which is acceptable — they live in `$XDG_RUNTIME_DIR` and are ephemeral.

3. **Resolve the root on the Lua side too.** In `daemon.lua`'s
   `M.socket_path(root)`, hash `vim.uv.fs_realpath(root) or root`, so a
   symlinked root does not re-split the two clients.

**Fold in the escape hatch.** Add `socket: str | Path | None = None` to
`AgentClient.__init__` and `--socket PATH` to the CLI callback. Five lines,
and it de-risks the whole step: if the derivations ever drift again, an agent
can still be pointed at a known daemon.

**Also fix F13 here.** Add `.git` to `_discover_root`'s markers
(`cli.py:50`), matching `config.lua:78`. Divergent root discovery is a second
route to two daemons.

**Verification gate**

```text
SECRETSPEC_REASON="p32f step1 build"  devenv shell -- build
SECRETSPEC_REASON="p32f step1 tests"  devenv shell -- pytest tests/test_daemon_root_lock.py tests/daemon
SECRETSPEC_REASON="p32f step1 nvim"   devenv shell -- test-nvim
```

New tests:
- Two `DaemonServer`s on one root with **different** socket paths: the second
  raises `DaemonAlreadyRunning`. `test_daemon_root_lock.py` currently hands
  both servers the same path, which is why it passes today.
- The Lua and Python socket derivations agree for one root (assert in the
  Neovim suite).
- `AgentClient(root, socket=…)` connects to an explicitly named socket.
- A root holding only `.git` is discovered by `_discover_root`.

`test-nvim` is the real gate for this step.

---

### Step 2 — An injectable cap, so the cap is testable (closes **F6**)

**The defect.** Every daemon test runs against the demo shop project, which
holds **9 entities** against a cap of 500 (`tests/daemon/conftest.py:30-37`).
No test can reach the cap, so Step 3's defect ships green.

**The fix.** Make the caps per-instance instead of module constants. Add
`max_symbols: int = _MAX_SYMBOLS` and `max_impact: int = _MAX_IMPACT` keyword
arguments to `Handlers.__init__` (`handlers.py:40`), store them, and read
`self._max_symbols` / `self._max_impact` in `symbols` (`handlers.py:526-531`)
and `impact` (`handlers.py:361-383`). Keep the module constants as the
defaults so nothing else changes.

*Why this beats generating a 500-entity fixture:* a test with `max_symbols=3`
on the shop project puts 6 of 9 ids outside the cap and runs in milliseconds.
Generating 500+ real entities costs seconds per test and still proves less.
Keep a generated large project for the benchmark suite only, if at all.

**Verification gate**

```text
SECRETSPEC_REASON="p32f step2 tests" devenv shell -- pytest tests/daemon
```

New test: `symbols` on a `Handlers(max_symbols=3)` sets `truncated` and
returns 3; the default instance clears `truncated` on the same fixture.

---

### Step 3 — Native durable-ID resolution (closes **F1**)

**The defect.** `AgentClient.context` resolves a durable ID by requesting
`symbols` with **no query** and scanning the response
(`client.py:134-146`). `symbols` caps at 500 and breaks *before* sorting
(`handlers.py:526-528`), so the 500 returned are an arbitrary graph-order
slice. Measured on a 173-file project: 2855 entities, 500 reachable, so
**82.5 % of valid durable IDs return `null`** after ~1.9 s of work — and the
CLI exits 0. `locate` resolves the same ID in ~0 ms and `impact` resolves it
correctly.

**The fix.** Give `context_pack` and `entity_at` the `durable_id` branch that
`impact` already has (`handlers.py:345-357`), and delete the client-side scan.

1. **`context_pack`** (`handlers.py:785-804`). Accept `durable_id` *or*
   `path`/`line`/`col`, with the same `INVALID_PARAMS` guard `impact` uses.

2. **`_gather_context`** (`handlers.py:922-973`). It needs `rel`, `line`,
   `col` for exactly one call: `s.find_references(...)` at `handlers.py:934-936`.
   **Move that call inside the `with s.snapshot()` block**, after `node` is
   resolved, and derive the anchor from `node.file` and `node.range.start`
   when the caller passed an ID.

   *Why move it rather than resolve the position in a second snapshot:* a
   second snapshot means a second graph build — measured at ~1.8 s. One
   snapshot serves both the anchor and every existing read.

3. **`entity_at`** (`handlers.py:147-168`). `_entity_dict` is already keyed
   purely on `did` (`handlers.py:977-1024`). Add the branch and skip the
   `s.id_for` call when an ID is given.

4. **Unknown or deleted IDs return `null`.** That is already the
   "nothing at this target" answer for `impact` (`handlers.py:355-357`) and
   for `locate` (`location: None`). Do not raise; do not invent a new error.
   Return `null` when `_node_by_id` finds no node.

5. **Return the exact entity the ID names.** No containment question exists —
   a durable ID names one entity node. `impact` already settles this.

6. **Client** (`client.py:134-146`). Delete the scan; forward `params`
   unchanged. Net removal.

7. **CLI.** No change. `_target` already emits `{"durable_id": …}`
   (`cli.py:109-123`).

**Do not** convert `definition`, `references`, `hover`, or `diagnostics_at` in
this step. They take a position because that is their LSP shape, the CLI does
not expose them, and widening them here buys nothing. That is Step 8.
**Do not** touch `check` — it takes a path, not an entity.

**Revision-evicted IDs are not reachable here.** These verbs read head. Only
`diff` time-travels, and it already raises `RevisionEvictedError`.

**Verification gate**

```text
SECRETSPEC_REASON="p32f step3 tests" devenv shell -- pytest tests/daemon
SECRETSPEC_REASON="p32f step3 nvim"  devenv shell -- test-nvim
```

New tests, using the Step 2 injectable cap:
- An ID from **beyond** the cap resolves through `context` and `entity_at`.
- Resolving by position and by ID yields the same result — mirror
  `test_impact_position_and_durable_id_match`.
- An unknown ID returns `null` from both verbs.
- Passing both a position and an ID, or neither, raises `INVALID_PARAMS`.
- The response still carries `revision` and every field the existing tests
  assert.

---

### Step 4 — Honest client failure semantics (closes **F3**, **F7**)

**Defect A (F3).** `_read_loop` exits on `OSError` without setting
`self._closed` (`client.py:286-290`), and the waiter's liveness guard reads
`self._closed` (`client.py:311`), so it never fires. A daemon that dies
mid-request blocks the client for the full timeout and then raises
`RequestTimeout` → exit **4** ("conflict or stale revision"). Measured: a
stub socket closed mid-request produced `RequestTimeout` after 10.00 s of a
10 s budget. With the default `timeout=30.0` that is a 30 s hang and a wrong
answer.

**Defect B (F7).** `_request` raises `RequestTimeout` without removing the
pending ID (`client.py:307-314`), and the late response is stored forever
(`client.py:280-283`). Measured: `len(_responses) == 1` after one timeout.
One leaked dict per timed-out request, for the life of the client.

**The fix.**

1. **Add a distinct `self._transport_dead = threading.Event()`.** Set it in
   `_read_loop`'s `finally`, before `notify_all`. Clear it in
   `_connect_socket`. In `_request`'s wait loop, raise `DaemonUnavailable`
   when either `_closed` or `_transport_dead` is set.

   *Why not just set `_closed`:* `close()` early-returns on
   `self._closed.is_set()` (`client.py:81-82`), so a reader-set `_closed`
   would turn a later explicit `close()` into a no-op and leak the socket.
   Two events, two meanings: "the caller closed us" and "the peer went away".

   *Ordering is already safe:* the wait loop tests
   `request_id not in self._responses` first, so a response that arrived just
   before EOF is still delivered.

2. **Track pending IDs.** Keep `self._pending: set[int]`. Add on send, discard
   on receipt, and discard on timeout. In `_read_loop`, store a response only
   when its ID is in `_pending`; otherwise drop it. This closes the leak and
   makes a stray or duplicated ID harmless.

3. **Keep "timeout means unknown".** Do not add cancellation, and do not
   reconnect after a timeout — the connection is healthy, only the deadline
   expired, and reconnecting would discard a result still in flight. The
   docstring at `errors.py:41-46` is correct; leave the semantic alone.

4. **Retry guidance is documentation, not code.** Reads are idempotent and
   safe to retry. `sync` is not: on timeout it may have committed, so an agent
   must reconcile with `status` and `changed` first. State that in Step 7.

**Verification gate**

```text
SECRETSPEC_REASON="p32f step4 tests" devenv shell -- pytest tests/daemon/test_agent_client.py
```

New tests:
- A stub `AF_UNIX` server that answers `ping`/`open` then closes the socket
  mid-request makes the client raise `DaemonUnavailable` in well under the
  client timeout. Assert on elapsed time, not just the type.
- `_responses` and `_pending` are both empty after a `RequestTimeout`, even
  once the late response has arrived.
- The existing timeout test (`test_agent_client.py:76-94`) still passes: the
  work still lands, and `status` still reconciles.

---

### Step 5 — One snapshot per revision (closes **F4**, the ~1.8 s read floor)

**The defect.** Every handler calls `s.snapshot()`, which returns a **fresh**
`Snapshot` (`session.py:419-438`). `Snapshot.graph()` caches per object
(`views.py:286-296`), so a new object per request means a full graph rebuild
per request.

Measured warm medians on the 173-file project: `symbols({})` 1980 ms,
`context_pack` 1954 ms, `entity_at` 1942 ms, `impact` 1466–1636 ms — against
`ping` 0.0 ms, `locate` 0.0 ms, `open` 8.5 ms, warm `check` 10.2 ms.

One `snap.graph()` decomposes as: `snapshot()` 9.1 ms, native
`full_code_delta()` 210.3 ms, `apply_code_delta()` 838.2 ms,
`refresh_diagnostics()` 733.6 ms, `_pin_at()` 3.3 ms. The second call on the
**same** snapshot costs 0.008 ms.

Over an already-built graph: `symbols` walk 0.81 ms, `transitive_dependents`
0.01 ms, durable-ID lookup 0.0004 ms.

**The fix.** Cache one `Snapshot` per revision on `Handlers`, and invalidate
it on every commit.

- Add `self._read_snap: Snapshot | None` and `self._read_snap_rev: int | None`.
- Add `_pinned(s) -> Snapshot`: return the cached snapshot when
  `s.head == self._read_snap_rev`; otherwise close the old one, open a new
  one, and record the revision.
- Replace `with s.snapshot() as snap:` with `snap = self._pinned(s)` in
  `symbols`, `entity_at`, `context_pack` / `_gather_context`, `impact`,
  `decorate`, `review_state`, and `layer_ids`. The cache owns the lifetime —
  do **not** close it inside a handler.
- Leave `diff` alone. It opens snapshots at explicit revisions
  (`handlers.py:254-280`) and must keep doing so.

**Why this is safe.** Every handler body runs inside `actor.submit`, and the
actor is a single owner thread (`session_actor.py:88-97`), so the cached
snapshot is never touched from two threads and never closed under a live
reader. Confirm that invariant holds for every call site you convert.

**Invalidation is the entire correctness argument. Write its test first.**
Keying on `s.head` is the simplest correct rule. If a commit can land without
moving `head`, key on the bus pump's commit signal instead.

**Fallback if snapshot caching destabilises the MVCC or rollback suites:**
use the session's already-cached head graph (`session.py:220-237`;
`session.graph` second call measured at 0.003 ms) for the graph-only reads,
and keep a fresh snapshot for the cross-layer joins. That recovers most of the
win and touches less. Prefer the snapshot cache; keep this in reserve.

**Verification gate**

```text
SECRETSPEC_REASON="p32f step5 build"  devenv shell -- build
SECRETSPEC_REASON="p32f step5 tests"  devenv shell -- tests
SECRETSPEC_REASON="p32f step5 nvim"   devenv shell -- test-nvim
```

New tests:
- Two reads inside one revision report the same `revision`.
- A read after a commit reports the **new** revision and the new content.
  Cover `symbols`, `entity_at`, `context_pack`, and `impact`.
- A read after `reindex` sees a file written with plain `pathlib` — extend the
  existing contract test.

New benchmark under `tests/benchmarks/`, marked `@pytest.mark.benchmark`,
calling `require_release_build()` first: record warm read cost after the
cache, against the numbers above. `devenv shell -- tests` must stay green —
the MVCC, rollback, snapshot, and bus suites are the real gate here.

---

### Step 6 — Machine-readable failure (closes **F9**, **P1-3**)

**Defect A (F9).** `cli.py:103-106` writes `tyo3-agent: {error}` to stderr as
prose and exits. `--json` does not change that. `error_type` reaches
`EngineError` (`client.py:350-362`) and then dies in-process. An agent that
gets exit 1 must parse English to learn what failed.

**Defect B.** Exit 4 means "conflict or stale revision" and currently also
carries `RequestTimeout` (`cli.py:92`). A timeout is neither. And no exit code
is documented anywhere — there is no match for "exit" in `README.md`.

**The fix.**

1. **Emit a versioned error object on stdout under `--json`.** In `_call`, on
   `AgentError`, when `options.json_output or not sys.stdout.isatty()`, write
   to **stdout**:

   ```json
   {"error": {"schema_version": 1, "type": "...", "message": "...",
              "method": "...", "exit_code": 1}}
   ```

   Take `type` from `EngineError.error_type` where present, else the exception
   class name. Keep the prose line on stderr as well — one stream for
   machines, one for humans. `schema_version` is the *CLI output* contract and
   is separate from the daemon's `protocol_version`.

2. **Give `RequestTimeout` exit code 5.** The table becomes: `0` success,
   `1` engine error, `2` usage error, `3` daemon unavailable, `4` conflict or
   stale revision (`RevisionEvicted`, `SessionClosed`), `5` timeout — result
   unknown.

   *Why move it now:* exit 4 tells an agent "your revision is stale, re-read".
   A timeout tells it "reconcile before you retry". They demand different
   recoveries. The MVP just shipped and the mapping was wrong anyway.

3. **Usage errors stay Typer-shaped.** `typer.BadParameter` is raised before
   `_call` reaches the client, so it keeps exit 2 and prose. That is fine —
   exit 2 is unambiguous and needs no payload. Do not add a custom Typer error
   handler for it.

**This breaks two existing assertions on purpose.**
`test_cli_usage_error_is_two_and_daemon_error_is_three` asserts
`unavailable_result.stdout == ""` (`test_agent_cli.py:85`). Update it to parse
the JSON error object. The usage-error assertion at `test_agent_cli.py:76`
stays as-is.

**Verification gate**

```text
SECRETSPEC_REASON="p32f step6 tests" devenv shell -- pytest tests/daemon/test_agent_cli.py
```

New tests:
- A failing command under `--json` emits a parseable error object carrying
  `type`, `method`, `exit_code`, and `schema_version`, with no ANSI codes.
- `DaemonUnavailable` exits 3; a `RequestTimeout` exits 5; an engine error
  exits 1; a usage error exits 2 with empty stdout.

---

### Step 7 — Correct the documentation (closes **F5**, **P1-6**)

`README.md:134-136` states the agent loop costs about 1.3 s and that "reads
are milliseconds". Measured on the 173-file project: `sync` 1686 ms, `check`
after that sync 681 ms, `find` 1962 ms. The millisecond claim holds only for
`ping`, `open`, `locate`, and a warm `check`.

**The fix.**

- Replace the cost paragraph with the measured numbers, before and after the
  Step 5 cache, and name the project size they came from. Cost tracks entity
  count, not file count — say so.
- Add the exit-code table from Step 6, with **"a timeout means the result is
  unknown, not cancelled"** stated beside it, and the retry rule: reads are
  safe to retry, `sync` is not — reconcile with `status` and `changed` first.
- State that `--id` is the stable target across edits and that positions go
  stale the moment the agent writes.
- State that one daemon serves a root and is **shared** with any attached
  editor, so the agent must not assume it owns the daemon it started.
- In `CONTRIBUTING.md`, note beside the existing build-profile gotcha that
  daemon reads are graph-backed and that the shop fixture is too small to
  expose a cap or a cost regression.

**Verification gate**

```text
SECRETSPEC_REASON="p32f step7 tests" devenv shell -- tests
SECRETSPEC_REASON="p32f step7 nvim"  devenv shell -- test-nvim
```

---

### Step 8 — The P2 batch (small separate lanes)

Land these individually. None blocks the others.

| Item | Fix | Finding |
|---|---|---|
| `gc` verb | Expose the existing `gc` route (`handlers.py:308-315`) through `AgentClient` and the CLI. No dry run, no confirmation — the artifacts it evicts are recomputable by definition. | F14 |
| Revision stamps | Add `revision` to `locate`, `layer_ids`, and `review_state`. Step 1.3 of the original project missed them. | F11 |
| Console script | `tyo3-agent` is registered unconditionally (`pyproject.toml:21`) but `cli.py:18` imports `typer`, which lives in the optional `cli` extra. Either move `typer-slim` into the runtime dependencies or fail with a clear message instead of `ImportError`. | F12 |
| Daemon stderr | `client.py:210` sets `stderr=DEVNULL`, so an agent-spawned daemon that crashes leaves no trail. Capture it into a bounded ring buffer and surface it on `status`, mirroring `daemon.lua:117-126`. | F10 |
| Durable IDs elsewhere | Accept `durable_id` on `definition`, `references`, `hover`, and `diagnostics_at`, reusing Step 3's resolver. | P2-7 |
| `tyo3-agent locate --id` | A thin wrapper over the existing `locate` route. | P2-8 |
| `--no-autostart` | Refuse to spawn; raise `DaemonUnavailable` when no daemon is listening. Useful in CI. | P2-3 |

**Verification gate per lane:** `devenv shell -- pytest tests/daemon`, plus
`devenv shell -- test-nvim` for any lane touching a daemon response shape
(the `gc` verb, the revision stamps, and the durable-ID widening all do).

---

## Explicitly out of scope

Do not build these. The investigation rejected each with evidence.

- **Identity-registry GC.** `rust/src/identity.rs:1010` retains `by_hash` for
  an orphan on purpose; that retention is what restores an entity's durable ID
  when it comes back. Collecting it trades the product's headline property for
  disk space. The existing `gc` touches derived artifacts only, and that is
  correct.
- **A file watcher in the agent plane.** The mechanism already exists and is
  off by default (`config.py:90`). Enabling it would commit revisions the agent
  never requested and break the explicit sync model the loop depends on.
- **`--autostop` as the default.** A shared daemon must outlive one CLI
  process, and reopening costs 3–4 s. Keep it as a flag for CI.
- **A `stop` verb that kills by default.** A client cannot currently see
  whether Neovim is attached. If a stop verb ever lands, gate it on a
  connected-client count reported by `ping`.
- **A batch or multi-verb CLI mode.** The 0.45 s per invocation is Python
  import cost, not RPC cost (`import tyo3.agent.cli` alone measured 406 ms).
  Step 5 removes the larger term. Revisit only with a measurement showing
  import cost dominates.
- **MCP or another tool protocol.** Nothing here requires it. Keep
  `AgentClient` adapter-ready and stop there.
- **Structured diagnostics.** Already done — `Diagnostic` carries `file`,
  `range`, `severity`, `code`, `message`, `details`
  (`models/analysis.py:104-114`).
- **Socket permissions and crash recovery.** Already correct. Socket 0600,
  parent 0700, uid-suffixed `/tmp` fallback (`server.py:58-74`,
  `server.py:296-299`); `flock` cannot go stale and `_bind` unlinks a leftover
  socket only under the lock (`server.py:284-292`).
- **`expected_revision` or any stale-mutation guard.** It protects
  `sync_buffers`, which agents must not call.

---

## Definition of done

1. A durable ID resolves through `context` and `entity_at` regardless of the
   symbol cap, and an unknown ID returns `null`.
2. Two daemons cannot own one root, whatever socket path either chose, and the
   Neovim and Python derivations agree.
3. A daemon that dies mid-request raises `DaemonUnavailable` (exit 3) at once,
   not `RequestTimeout` after the full deadline.
4. A timed-out request leaves no pending state behind.
5. Graph-backed reads cost milliseconds on a warm revision, and a read after a
   commit reports the new revision.
6. A failing command under `--json` emits a versioned, parseable error object,
   and every exit code is documented.
7. The README's cost figures match a reproducible release-build measurement.
8. `tests`, `test-nvim`, `check-rust`, `clippy`, and `parity-oracle` are all
   green, and the three contract rules are unchanged.

## Full verification before each lane lands

```text
SECRETSPEC_REASON="<step> release build" devenv shell -- build
SECRETSPEC_REASON="<step> check-rust"    devenv shell -- check-rust
SECRETSPEC_REASON="<step> clippy"        devenv shell -- clippy
SECRETSPEC_REASON="<step> parity oracle" devenv shell -- parity-oracle
SECRETSPEC_REASON="<step> tests"         devenv shell -- tests
SECRETSPEC_REASON="<step> nvim"          devenv shell -- test-nvim
SECRETSPEC_REASON="<step> pre-save"      devenv shell -- gitman status
```
