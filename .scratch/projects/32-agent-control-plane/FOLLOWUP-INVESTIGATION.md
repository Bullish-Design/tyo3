# Project 32 — follow-up investigation

**Status: investigation only. No code changed. 2026-09-14.**

What to add to the headless agent/CLI plane after the Project 32 MVP.

[KICKOFF.md](KICKOFF.md) stays authoritative for the original scope.
[IMPLEMENTATION.md](IMPLEMENTATION.md) is authoritative for the work already
done. [INVESTIGATION.md](INVESTIGATION.md) predates measurement and is not a
roadmap. This document adds measured evidence for the *next* scope.

The three contract rules are unchanged and are not re-opened here:

1. The agent owns the filesystem.
2. TyO3 never writes source files.
3. The agent never sends overlay text.

---

## 1. Current-state map

```
  tyo3-agent (CLI)                  src/tyo3/agent/cli.py       248 lines
    │  one Typer command per verb; renders JSON or a key/value table
    ▼
  AgentClient (sync client)         src/tyo3/agent/client.py    365 lines
    │  socket resolve → connect-or-spawn → framing → id correlation
    │  typed errors                 src/tyo3/agent/errors.py     51 lines
    ▼  newline-delimited JSON-RPC 2.0 over AF_UNIX
  DaemonServer                      src/tyo3/daemon/server.py   474 lines
    │  flock per socket path, accept loop, 8-thread dispatch pool, bus pump
    ▼
  Handlers (31 methods)             src/tyo3/daemon/handlers.py 1308 lines
    ▼  every call through one owner thread
  SessionActor → TyO3Session → native engine
```

### Facts about each layer

| Layer | Fact | Evidence |
|---|---|---|
| CLI | 9 verbs; no `--socket`, no `stop`, no `gc`, no `locate` | `cli.py:133-244` |
| CLI | Errors print as prose on stderr; `--json` does not apply to them | `cli.py:103-106` |
| CLI | Exit codes 0/1/2/3/4 are assigned in code, documented nowhere | `cli.py:89-96`; no match for "exit" in `README.md` |
| CLI | Root discovery accepts `pyproject.toml` or `.tyo3`, not `.git` | `cli.py:46-52` |
| Client | Socket path is `default_socket_path(root)`; no override | `client.py:48` |
| Client | Spawns `tyo3-daemon` with `stderr=DEVNULL` | `client.py:206-213` |
| Client | Never stops a daemon it spawned | `client.py:79-80` |
| Client | `context(durable_id=…)` scans an uncapped `symbols` call | `client.py:134-146` |
| Client | Records `restarted` on instance change; never raises | `client.py:337-343` |
| Daemon | `locate(durable_id)` already exists and is registered | `handlers.py:244-252`, `handlers.py:1280` |
| Daemon | `gc` already exists and is registered | `handlers.py:308-315`, `handlers.py:1283` |
| Daemon | `impact` already accepts `durable_id` natively | `handlers.py:334-394` |
| Daemon | `symbols` caps at 500, reports `truncated` | `handlers.py:491-533`, `handlers.py:1084` |
| Daemon | Root lock is `socket_path.with_suffix(".lock")` | `server.py:133`, `server.py:229-257` |
| Daemon | `--autostop` exists; neither client nor Neovim uses it | `__main__.py:30-34` |
| Engine | A file watcher exists, off by default | `config.py:90`, `tests/test_watch.py` |
| Engine | Orphaned identity anchors are retained on purpose | `rust/src/identity.rs:827-831`, `rust/src/identity.rs:1010` |

### Measurement conditions

All numbers come from a **release** build (`tyo3.build_profile() == "release"`,
verified at the top of every script through `require_release_build()`).

The project under test is `/tmp/tyo3-bench`: a copy of this repository's
`src/`, `tests/` and `fixtures/`. It holds **173 Python files, 3457 graph
nodes, and 2855 entity nodes**. `.tyo3/identity.db` was removed before each
run. Ambient `mypi`/`MYPI` setup noise is filtered out of every capture.

---

## 2. Findings

### Observed facts

**F1 — `context --id` silently fails for 82.5 % of durable IDs.**

`AgentClient.context` resolves a durable ID by requesting `symbols` with **no
query** and scanning the response (`client.py:138-141`). `symbols` caps at 500
entries and breaks *before* sorting (`handlers.py:526-528`), so the returned
500 are an arbitrary slice in graph-node-index order.

Measured on the 173-file project:

```
symbols: count=500 truncated=True limit=500
entity ids total=2855  in capped symbols=500  outside=2355
client context(--id) INSIDE cap :   3478.6 ms  result=dict
client context(--id) OUTSIDE cap:   1873.0 ms  result=None
locate(outside) -> /tmp/tyo3-bench/src/tyo3/daemon/handlers.py::Handlers::methods
impact(outside) -> dict
```

The ID is valid. `locate` resolves it. `impact` resolves it. `context` returns
`null` after 1.9 s of work. The CLI renders `null` and exits 0. An agent
cannot tell "no such entity" from "your entity was outside an arbitrary cap".

The daemon already owns the right primitive. `locate` costs **~0 ms**
(`0.0 ms` median over 5 warm calls; 92 µs measured directly at the session
level). `impact` already takes `durable_id` and resolves it through
`_node_by_id` (`handlers.py:352-357`). `context_pack` and `entity_at` do not.

**F2 — Two daemons can own one project root.**

`server.py:229-241` documents the invariant: exactly one daemon per root,
enforced by an `flock`. The lock file is derived from the **socket path**
(`server.py:133`), not from the root.

The two clients derive that path differently:

| Client | Derivation | Path for this repo |
|---|---|---|
| `AgentClient` / `tyo3-daemon` | `sha1(resolved_root)[:16]` (`server.py:57`) | `2edb02364f095beb.sock` |
| `tyo3.nvim` | `sha256(root)[:16]` (`daemon.lua:41-44`) | `c7f8c5e0d8fa20d8.sock` |

Both live in `$XDG_RUNTIME_DIR/tyo3/`. Neovim passes its path through
`--socket` (`daemon.lua:57`), which overrides the default.

Measured directly (`M3`):

```
socket A (python default): 1e0dfc594c38ebc5.sock
socket B (nvim --socket) : 411cdec6481434ed.sock
lock A: 1e0dfc594c38ebc5.lock  lock B: 411cdec6481434ed.lock
RESULT: BOTH daemons acquired the lock on ONE root
        -> two writable sessions over one .tyo3/
```

`tests/test_daemon_root_lock.py` passes because every case passes the **same**
`socket_path` to both servers. It tests the lock, not the invariant the lock
exists to protect.

Consequence: running `tyo3-agent` on a project that Neovim already has open
starts a second daemon. Both hold writable sessions over one `.tyo3/` sidecar
and interleave writes to one identity registry — the exact hazard named in
`tests/test_daemon_root_lock.py:1-12`.

**F3 — A dead daemon is reported as a conflict, one full timeout late.**

`_read_loop` exits on `OSError` without setting `self._closed`
(`client.py:286-290`). The waiter's liveness guard reads `self._closed`
(`client.py:311`), so it never fires. Measured against a stub daemon that
closes the socket mid-request, with a 10 s client timeout:

```
B. after 10.00s (client timeout=10.0s) -> RequestTimeout: request 'check' timed out after 10s; result is UNKNOWN
exit code for RequestTimeout  : 4
exit code for DaemonUnavailable: 3
```

With the default `timeout=30.0`, a crashed daemon hangs the CLI for 30 s and
then exits 4 ("conflict or stale revision"). The correct answer is exit 3
("daemon unavailable"), immediately.

**F4 — Every graph-backed read rebuilds the whole graph.**

Each handler calls `s.snapshot()`, which returns a **fresh** `Snapshot`
(`session.py:419-438`). `Snapshot.graph()` caches per object
(`views.py:286-296`), so a new object per request means a new build per
request.

Warm medians on the 173-file project:

| Read | Cost |
|---|---|
| `ping` | 0.0 ms |
| `locate(durable_id)` | 0.0 ms |
| `open` | 8.5 ms |
| `check()` warm | 10.2 ms |
| `impact(position)` | 1465.5 ms |
| `impact(durable_id)` | 1635.9 ms |
| `symbols(query=…)` | 1866.8 ms |
| `entity_at(position)` | 1942.0 ms |
| `context_pack(position)` | 1954.1 ms |
| `symbols({})` | 1980.5 ms |

Decomposition of one `snap.graph()`:

| Step | Cost |
|---|---|
| `session.snapshot()` | 9.1 ms |
| native `full_code_delta()` | 210.3 ms |
| `CodeGraph.apply_code_delta()` | 838.2 ms |
| `graph.refresh_diagnostics()` | 733.6 ms |
| `graph._pin_at()` | 3.3 ms |
| **first `snap.graph()`** | **1765.1 ms** |
| second `snap.graph()`, same snapshot | 0.008 ms |
| `session.graph` first (build-on-demand) | 1845.7 ms |
| `session.graph` second (cached) | 0.003 ms |

Reads over an already-built graph are free:

| Operation over a cached graph | Cost |
|---|---|
| graph build, once per revision | 1024.8 ms |
| `symbols`-shaped entity walk | 0.81 ms |
| `transitive_dependents(id)` | 0.01 ms |
| `durable_id` → node | 0.0004 ms |

The engine is not slow. The daemon discards the build after every request.

**F5 — The README read-cost claim is wrong for the agent's main reads.**

`README.md:134-136` states "reads are milliseconds". That holds for `ping`,
`open`, `locate`, and a warm `check`. It does not hold for `find`, `context`,
`impact`, or `entity_at`, which cost 1.4–2.0 s each on this project.

Measured agent loop on the same project:

```
agent loop: sync (reindex, 1 new file)             1686 ms  created=2
agent loop: check after sync                        681 ms
agent loop: find (symbols query)                   1962 ms
```

The README's "about 1.3 s per iteration" understates a loop that includes any
graph-backed read. `sync` + `check` alone measured 2.4 s here, because the
commit invalidates the check cache (the 10.2 ms figure is the warm repeat).

**F6 — Every existing agent test runs against a 9-entity project.**

```
shop fixture: symbols=9 truncated=False (cap 500)
```

`tests/daemon/conftest.py:30-37` builds the demo shop project for every daemon
test. Nine entities against a cap of 500. No test can reach the cap, so F1 is
invisible to the suite.

**F7 — A timed-out request leaks its response forever.**

`_request` raises `RequestTimeout` without removing the pending ID
(`client.py:307-314`). The late response arrives and is stored
(`client.py:280-283`). Nothing pops it. Measured:

```
A. timeout raised: RequestTimeout
A. _responses retained after timeout: dict_keys([3])
A. len(_responses): 1
```

One leaked dict per timed-out request, for the life of the client.
`reconnect()` clears the map; `close()` does not.

**F8 — CLI process overhead is ~0.45 s per invocation.**

Against a resident daemon on the 9-entity shop project:

```
cli status wall: 463 ms
cli find wall:   472 ms
cli check wall:  442 ms
import tyo3.agent.cli only:      406 ms
cli --help (no daemon contact):  456 ms
```

Almost all of it is the Python import that loads the 33 MB native extension.
The RPC round trip is 30–60 ms. A six-command agent loop pays ~2.7 s of
interpreter start-up.

**F9 — Machine-readable errors do not exist at the CLI boundary.**

`cli.py:103-106` writes `tyo3-agent: {error}` to stderr as prose and exits.
`--json` does not change this. `error_type` reaches `EngineError`
(`client.py:350-362`) and then dies inside the process. An agent that gets
exit 1 must parse English to learn what failed.

**F10 — Agent-spawned daemon logs are discarded.**

`client.py:210` sets `stderr=subprocess.DEVNULL`. Neovim captures the same
stream into a 200-line ring buffer (`daemon.lua:117-126`). A daemon that
crashes under an agent leaves no diagnostic trail.

**F11 — Three agent-facing responses carry no revision.**

Step 1.3 stamped the read surface. `locate` (`handlers.py:244-252`),
`layer_ids` (`handlers.py:723-746`, behind `notes`), and `review_state`
(`handlers.py:748-782`, behind `notes --stale`) return unstamped payloads.

**F12 — The `tyo3-agent` script has an unsatisfiable dependency.**

`pyproject.toml:21` registers `tyo3-agent` unconditionally. `cli.py:18`
imports `typer` unconditionally. `typer-slim` sits in the optional `cli`
extra (`pyproject.toml:23-26`). A plain `pip install tyo3` yields a
`tyo3-agent` entry point that raises `ImportError`.

**F13 — Root discovery diverges from Neovim.**

CLI markers are `pyproject.toml` and `.tyo3` (`cli.py:50`). Neovim markers are
`.tyo3`, `pyproject.toml`, and `.git` (`config.lua:78`). In a repository whose
root has `.git` but no `pyproject.toml`, the two pick different roots — a
second way to reach F2.

**F14 — Identity-registry orphans are retained by design.**

`session.gc()` evicts orphaned *derived artifacts* only, and only for layers
declaring `gc = "orphans"` (`session.py:723-733`, `derive/dag.py:226-266`).
There is no identity-registry purge. `rust/src/identity.rs:1010` asserts
`by_hash` is **retained** for an orphan, because that retention is what lets a
re-added entity recover its durable ID.

The `.tyo3/identity.db` deletion named as a benchmark hazard is a reset, not a
collection.

### Inferred risks

- **R1 (from F2).** Two writable sessions over one identity registry can
  interleave sidecar writes. The consequence is not measured here and is not
  cheap to measure safely. Treat it as a correctness hazard, not a proven
  corruption. The fix removes the possibility, so measuring it is unnecessary.
- **R2 (from F4).** Read cost tracks entity count. The shop fixture cannot
  detect a regression. Any future read added to the agent plane inherits the
  ~1 s floor invisibly.
- **R3 (from F3 + F7).** A long-lived `AgentClient` that hits repeated
  timeouts against a dead daemon accumulates leaked responses while reporting
  the wrong failure class. Neither leak nor misclassification self-heals.
- **R4 (from F8).** Any per-command batching design must not introduce a
  persistent client that re-opens the F2 ownership question.

---

## 3. Behaviour matrix

### Candidate 1 — Native durable-ID context resolution

| Dimension | Assessment |
|---|---|
| Current behaviour | `AgentClient.context` scans an uncapped `symbols` response (`client.py:138-141`). 500 of 2855 IDs resolve. The rest return `null` after ~1.9 s. `impact` already resolves any ID natively. |
| User impact | Silent wrong answer on every project above 500 entities. Indistinguishable from "entity not found". Exit code 0. |
| Protocol changes | Additive only. Accept `durable_id` on `context_pack` and `entity_at`, exactly as `impact` already does (`handlers.py:345-350`). No response shape changes. No existing field changes meaning. |
| Client changes | Delete the scan. Forward `durable_id` to the handler. Net removal of code. |
| CLI changes | None. `_target` already emits `{"durable_id": …}` (`cli.py:109-123`). |
| Compatibility risk | None. Position-keyed calls are untouched. Neovim never sends `durable_id` to these verbs. |
| Performance | One graph build instead of two: 3479 ms → ~1900 ms today, → ~1 ms once Candidate 5 lands. |
| Which entity to return | The exact entity the ID names. `_node_by_id` returns one node. No containment ambiguity exists — `impact` already settles this. |
| Unknown / deleted IDs | Return `null`, matching `impact` (`handlers.py:355-357`) and `locate` (`location: None`, measured). Do not raise: `null` is already the "nothing at this target" answer across the read surface. |
| Stale / revision-evicted IDs | Not reachable. These verbs read head. Only `diff` time-travels, and it already raises `RevisionEvictedError`. |
| Which verbs | `context_pack` and `entity_at` are required (the CLI exposes them). `definition`, `references`, `hover`, `diagnostics_at` take a position because that is their LSP shape; converting them is P2, not P0. `check` takes a path, not an entity — leave it. |
| A distinct `resolve` verb | Not needed. `locate` is the primitive and is already registered. Keep resolution implicit in the verb the agent actually wants. Exposing `tyo3-agent locate` is a convenience, not a requirement. |
| Test obligations | A fixture above the symbol cap, or a cap override. An ID from beyond the first 500 must resolve. A `null` for an unknown ID. Position and ID must agree, as `test_impact_position_and_durable_id_match` already asserts for `impact`. |
| Documentation | State that `--id` is the stable target across edits and that positions go stale. |

### Candidate 2 — Daemon lifecycle and ownership

| Dimension | Assessment |
|---|---|
| Current behaviour | The client connects or spawns, then always leaves the daemon resident (`client.py:79-80`). No stop, no `--socket`, no `--no-autostart`. The socket path disagrees with Neovim's (F2). |
| User impact | Two daemons on one root when Neovim is attached. A CI job leaves a resident daemon holding a session. No way to point the agent at a known socket. |
| Protocol changes | None required for the ownership fix. A client count on `ping` would be additive, and is only needed if a `stop` verb lands. |
| Client changes | Accept an explicit `socket` path. Accept `autostart=False`. |
| CLI changes | `--socket PATH` and `--no-autostart` on the shared callback. |
| Compatibility risk | **The ownership fix has a real one.** Changing either hash orphans live daemons and live Neovim sessions until they restart. Unifying on the Python default (`sha1`) means `tyo3.nvim` must change `socket_path`; unifying on Neovim's (`sha256`) means `default_socket_path` must change. Either way, `test-nvim` is the gate. |
| Performance | None. Path derivation is not on any hot path. |
| Ownership model | **One daemon per root, shared by all clients for that root.** This is already the design (`server.py:229-241`); the bug is that the key is wrong. Do not add per-client ownership. |
| Stop / restart | Do not add a `stop` that kills by default. A client cannot currently see whether Neovim is attached. If a stop verb lands, gate it on a connected-client count reported by `ping`. |
| Stale sockets and crashes | Already correct. `flock` is released by the kernel on any exit, and `_bind` unlinks a leftover socket only while holding the lock (`server.py:284-292`). No pid probe is needed. Fixing F2 makes this protection real. |
| Idle timeout | `--autostop` already exists (`__main__.py:30-34`). Do not make it the default: a shared daemon must outlive one CLI process, and re-opening costs 3–4 s (measured `actor.start`). It is the right flag for CI. |
| Instance IDs | `instance_id` already detects restart (`handlers.py:48`). The client flags it but never raises (`client.py:337-343`). A CLI process is too short-lived for the flag to fire; an agent must compare `instance_id` across `status` calls itself. Document that. |
| Where lifecycle belongs | In `AgentClient`, where it is. A separate supervisor duplicates `daemon.lua` for no gain. |
| Test obligations | Two `DaemonServer`s on one root with **different** socket paths must refuse the second — the test `test_daemon_root_lock.py` does not currently write. A Neovim-derived path and a Python-derived path for one root must be equal. |
| Documentation | State that one daemon serves a root and that the agent shares it with any attached editor. |

### Candidate 3 — Timeout and failure semantics

| Dimension | Assessment |
|---|---|
| Current behaviour | Exit 4 covers `RevisionEvicted`, `SessionClosed`, and `RequestTimeout` (`cli.py:92`). A transport loss becomes `RequestTimeout` after the full timeout (F3). Late responses leak (F7). Errors are prose (F9). |
| User impact | A crashed daemon hangs 30 s then reports a conflict. An agent cannot branch on failure without parsing English. |
| Is exit 4 documented? | No. No occurrence of "exit" in `README.md`. The codes exist only in `cli.py:89-96` and `KICKOFF.md`. |
| Does timeout mean unknown? | Yes, and the code says so correctly (`errors.py:41-46`). The client does **not** cancel, and the daemon has no cancellation path. The test at `test_agent_client.py:76-94` already proves the work lands. Keep this semantic; do not add cancellation. |
| Late responses | Retained forever (F7). Fix: pop the pending ID on timeout and have `_read_loop` discard responses for unknown IDs. |
| Reconnect after timeout? | No. The connection is healthy; only the deadline expired. Reconnecting would discard a result that is still arriving. |
| Distinct error classes | Yes, for one case: transport loss must raise `DaemonUnavailable`, not `RequestTimeout`. Set `_closed` when the reader loop exits. `RevisionEvicted` and `SessionClosed` are already distinct classes sharing exit 4; splitting `RequestTimeout` off to its own code is the honest mapping — 4 means "conflict or stale revision", and a timeout is neither. |
| Retry safety | Reads are idempotent and safe to retry. `sync` is not: on timeout it may have committed, so an agent must reconcile with `status` and `changed` before retrying. This is already documented in the docstring; it is not in the README. |
| stderr vs stdout | Diagnostics on stderr, results on stdout, as now. Add a machine-readable error object on **stdout** under `--json`, so an agent reads one stream. |
| Stable error schema | Yes. `{"error": {"type": …, "message": …, "method": …, "exit_code": …}}` with the existing `protocol_version` alongside it. `error_type` already crosses the wire (`server.py:400-409`). |
| Protocol changes | None. Every change is client- and CLI-side. |
| Performance | None. |
| Test obligations | A stub socket that closes mid-request must yield `DaemonUnavailable` promptly, not `RequestTimeout` after the deadline. `_responses` must be empty after a timeout. `--json` on a failing command must emit a parseable error object and the documented code. |
| Documentation | An exit-code table in `README.md`, with "timeout means unknown, not cancelled" stated next to it. |

### Candidate 4 — Garbage collection and maintenance

| Dimension | Assessment |
|---|---|
| Current behaviour | `gc` exists in the daemon and is registered (`handlers.py:308-315`). The agent plane does not expose it. |
| What it removes | Orphaned derived artifacts, and only for layers declaring `gc = "orphans"` (`derive/dag.py:266`). Nothing else. |
| What it does **not** remove | Identity records, notes, revisions, caches. None of these has a collection path. |
| Can GC alter durable-ID stability? | The existing `gc` cannot — it never touches the registry. A *new* identity GC **would**, and that is the reason to reject it. `rust/src/identity.rs:1010` retains `by_hash` for an orphan on purpose; that retention is what restores an entity's ID when it comes back. Collecting it would trade the product's headline property for disk space. |
| User impact of exposing the existing `gc` | Small and safe. It closes a stated roadmap gap at near-zero cost. |
| Protocol changes | None. The route exists. |
| Client changes | One method. |
| CLI changes | One verb. |
| Dry-run / confirmation | Not warranted for derived-artifact eviction — the artifacts are recomputable by definition. Do not add a confirmation prompt to a headless CLI. |
| Output and exit status | `{"ok": true, "revision": …}` as the handler already returns; exit 0. Report the count evicted if `gc_orphans` can be made to return one; otherwise leave the shape alone. |
| Where maintenance belongs | With the agent CLI. There is no daemon administration API and this investigation found no case for one. |
| Test obligations | `gc` reachable through the client and the CLI. `test_gc_is_ok` already covers the handler. |
| Documentation | State plainly what `gc` collects and — more importantly — that identity records are **never** collected, and why. |

### Candidate 5 — Per-revision read cache (found during this investigation)

| Dimension | Assessment |
|---|---|
| Current behaviour | Each graph-backed request builds a fresh `CodeGraph` and refreshes diagnostics: 1765 ms (F4). |
| User impact | `find`, `context`, `impact`, and `entity_at` cost 1.4–2.0 s each on a mid-size project. The README promises milliseconds. |
| Protocol changes | **None.** This is a daemon-internal cache. |
| Client changes | None. |
| CLI changes | None. |
| Compatibility risk | Low, but real: the cached snapshot must be dropped on every commit, or reads serve a stale revision. The bus pump and `_after_commit` are the existing invalidation points. Correctness rests entirely on that hook. |
| Performance | Measured ceiling: one 1025 ms build per revision, then 0.81 ms per `symbols` walk, 0.01 ms per `transitive_dependents`, 0.0004 ms per ID lookup. |
| Test obligations | A read after a commit must report the new revision. Two reads within one revision must report the same revision. Existing revision-stamping tests already assert the shape; they need a commit-in-between case. |
| Documentation | Correct the README read-cost claim either way. |

---

## 4. Prioritised recommendation

### P0 — required for correctness or contract integrity

- **P0-1 (F1).** Accept `durable_id` on `context_pack` and `entity_at`; delete
  the client-side symbol scan. Today 82.5 % of durable IDs silently resolve to
  `null`.
- **P0-2 (F2).** Key the daemon root lock on the **root**, and make the Neovim
  and Python socket derivations agree. Two daemons per root defeats the
  invariant the lock exists to enforce.
- **P0-3 (F3).** Set `_closed` when the reader loop exits, so a transport loss
  raises `DaemonUnavailable` (exit 3) at once instead of `RequestTimeout`
  (exit 4) after the full deadline.

### P1 — high-value next addition

- **P1-1 (F4/F5).** Cache one snapshot and its graph per revision in the
  daemon. Reads drop from ~1.9 s to ~1 ms. No protocol change.
- **P1-2 (F9).** Emit a machine-readable error object on stdout under `--json`,
  carrying `error_type`, `method`, and the exit code.
- **P1-3 (F3).** Document the exit codes in `README.md`, and split
  `RequestTimeout` off exit 4 into its own code. A timeout is not a conflict.
- **P1-4 (F6).** Add a fixture that exceeds the symbol cap. Without it, P0-1
  cannot be regression-tested and R2 stays invisible.
- **P1-5 (F7).** Pop the pending ID on timeout; discard unknown-ID responses in
  the reader.
- **P1-6 (F5).** Correct the README read-cost and loop-cost claims.

### P2 — useful but deferrable

- **P2-1 (F14).** Expose the existing `gc` as `tyo3-agent gc`, and document
  that identity records are never collected.
- **P2-2 (F11).** Stamp `revision` on `locate`, `layer_ids`, and `review_state`.
- **P2-3.** Add `--socket` and `--no-autostart` to the client and CLI.
- **P2-4 (F12).** Make the `tyo3-agent` console script depend on its extra, or
  fail with a clear message instead of `ImportError`.
- **P2-5 (F13).** Add `.git` to CLI root discovery, matching Neovim.
- **P2-6 (F10).** Capture spawned-daemon stderr into a ring buffer for
  `status`, mirroring `daemon.lua:117-126`.
- **P2-7.** Accept `durable_id` on `definition`, `references`, `hover`, and
  `diagnostics_at`.
- **P2-8.** Expose `tyo3-agent locate --id` as a thin wrapper over `locate`.
- **P2-9.** Explicit revision pinning (`--at-revision`). The engine supports it
  (`session.snapshot(at=…)`); no read verb exposes it. It only matters when a
  second client commits mid-loop, which today means an attached editor.

### Reject or defer

- **Identity-registry GC.** Rejected. It would weaken durable-ID stability,
  which is the product. See F14.
- **A file watcher in the agent plane.** Rejected. The mechanism already exists
  and is off by default (`config.py:90`). Enabling it would commit revisions
  the agent did not request and break the explicit sync model the loop depends
  on.
- **MCP or another tool protocol.** Deferred. Nothing in this investigation
  requires it. `AgentClient` stays adapter-ready.
- **`--autostop` by default.** Rejected. A shared daemon must outlive one CLI
  process, and reopening costs 3–4 s. Keep the flag for CI.
- **A batch or multi-verb CLI mode.** Deferred. The 0.45 s per invocation (F8)
  is Python import cost, not RPC cost. Fix the 1.9 s reads first (P1-1); that
  is the larger term. Revisit only with a measurement showing import cost
  dominates.
- **Structured diagnostics and source locations.** Already done. `Diagnostic`
  carries `file`, `range`, `severity`, `code`, `message`, and `details`
  (`models/analysis.py:104-114`).
- **Socket permissions.** Already correct. Socket 0600, parent 0700, uid-suffixed
  `/tmp` fallback (`server.py:58-74`, `server.py:296-299`).
- **Crash recovery.** Already correct. `flock` cannot go stale; `_bind` unlinks
  a leftover socket only under the lock (`server.py:229-241`, `server.py:284-292`).

---

## 5. Proposed implementation plan

One lane per step. Verify before landing. Run every command inside
`devenv shell` with a `SECRETSPEC_REASON`. Route all version control through
`gitman`. Use `build` for release; `build-debug` only for debug. Delete
`.tyo3/identity.db` before any benchmark. Filter ambient `mypi`/`MYPI` noise.

### Step 1 — A fixture above the symbol cap (P1-4)

Every later step needs it. Land it first.

Add a fixture that builds a project with more than 500 entities, or make
`_MAX_SYMBOLS` injectable so a test can lower the cap. Prefer the injectable
cap: generating 500+ entities costs seconds per test.

**Gate:** `devenv shell -- pytest tests/daemon` green. A new test asserts
`symbols({})` sets `truncated` on the new fixture and clears it on the shop
fixture.

### Step 2 — Durable-ID resolution in the handlers (P0-1)

Add a `durable_id` branch to `context_pack` and `entity_at`, mirroring
`impact` (`handlers.py:345-357`). Resolve the node with `_node_by_id`; take
`path`, `line`, and `col` from `node.file` and `node.range.start` for the one
call that needs them (`find_references` in `_gather_context`, `handlers.py:936`).
Return `null` for an unknown ID. Delete `client.py:136-145`.

**Gate:** `devenv shell -- pytest tests/daemon`. A test resolves an ID from
beyond the cap on the Step 1 fixture. A test asserts position and ID agree.
`devenv shell -- test-nvim` — the response shape is unchanged, but the read
surface moved.

### Step 3 — Root-keyed daemon lock and one socket path (P0-2)

Derive the lock path from the resolved root, not the socket path. Unify the
two socket derivations. Decide the direction first — changing
`default_socket_path` orphans running daemons; changing `daemon.lua` orphans
running Neovim sessions. Changing `daemon.lua` is the smaller blast radius,
because Neovim already passes `--socket` explicitly and a plugin reload is
cheaper than a daemon restart.

**Gate:** `devenv shell -- pytest tests/test_daemon_root_lock.py` with a new
case: two servers, one root, **different** socket paths, second refused.
A test asserts the Lua and Python derivations agree for one root.
`devenv shell -- test-nvim` is the real gate here.

### Step 4 — Transport-loss classification and the response leak (P0-3, P1-5)

Set `self._closed` in the reader loop's `finally`. Pop the pending ID before
raising `RequestTimeout`. Discard responses whose ID is not pending.

**Gate:** `devenv shell -- pytest tests/daemon/test_agent_client.py`. A stub
socket that closes mid-request raises `DaemonUnavailable` in well under the
client timeout. `_responses` is empty after a timeout.

### Step 5 — Per-revision read cache in the daemon (P1-1)

Hold one `Snapshot` per revision on the handlers or the actor. Invalidate on
every commit. Serve `symbols`, `entity_at`, `context_pack`, `impact`,
`decorate`, `review_state`, and `layer_ids` from it.

Invalidation is the whole correctness argument. Write that test before the
cache.

**Gate:** `devenv shell -- pytest tests/daemon`. A read after a commit reports
the new revision. Two reads inside one revision report the same revision.
`devenv shell -- test-nvim`. A benchmark under `tests/benchmarks/` records the
warm read cost after `require_release_build()`.

### Step 6 — Machine-readable errors and the exit-code table (P1-2, P1-3)

Emit `{"error": {"type", "message", "method", "exit_code"}}` on stdout under
`--json`. Give `RequestTimeout` its own exit code. Document all codes in
`README.md` beside the statement that a timeout means unknown.

**Gate:** `devenv shell -- pytest tests/daemon/test_agent_cli.py`. A failing
command under `--json` emits a parseable error object and the documented code.
Update `test_cli_usage_error_is_two_and_daemon_error_is_three`.

### Step 7 — Documentation corrections (P1-6)

Correct the README read-cost and loop-cost claims with the numbers in §2.
State that `--id` is the stable target. State that one daemon serves a root and
is shared with any attached editor.

**Gate:** `devenv shell -- tests` and `devenv shell -- test-nvim` green.

### Step 8 — P2 batch

Land `gc`, the three missing revision stamps, `--socket`, `--no-autostart`,
the `.git` root marker, the console-script dependency fix, and spawned-daemon
stderr capture as small separate lanes.

**Gate per lane:** `devenv shell -- pytest tests/daemon`, plus
`devenv shell -- test-nvim` for any lane touching a daemon response shape.

### Full verification before each lane lands

```text
SECRETSPEC_REASON="<step> release build" devenv shell -- build
SECRETSPEC_REASON="<step> check-rust"    devenv shell -- check-rust
SECRETSPEC_REASON="<step> clippy"        devenv shell -- clippy
SECRETSPEC_REASON="<step> parity oracle" devenv shell -- parity-oracle
SECRETSPEC_REASON="<step> tests"         devenv shell -- tests
SECRETSPEC_REASON="<step> nvim"          devenv shell -- test-nvim
SECRETSPEC_REASON="<step> pre-save"      devenv shell -- gitman status
```

---

## 6. Is the current MVP safe to use before this work?

**Yes, with three named limits.**

The contract holds. No daemon route writes a source file. `rename` returns
edits and applies none (`handlers.py:638-660`). `author` writes only to the
`.tyo3/` sidecar. `sync_buffer` overlays in memory. The agent plane exposes no
overlay verb (`client.py:29-36`), and the agent-owned-file contract test passes
(`test_agent_client.py:47-61`). `tests/test_final_no_read_side_writes.py` keeps
reads side-effect-free. None of the findings above touches these rules.

The limits:

1. **Use `tyo3-agent context <path>:<line>:<col>`, not `--id`, on any project
   above 500 entities.** `--id` returns `null` for 82.5 % of valid IDs on the
   measured project and exits 0 (F1). `impact --id` is unaffected and correct.
2. **Do not run `tyo3-agent` on a project that Neovim already has open.** It
   starts a second daemon on the same root, and both hold writable sessions
   over one identity registry (F2).
3. **Expect ~2 s per graph-backed read, and a 30 s hang if the daemon dies.**
   `find`, `context`, `impact`, and `entity_at` cost 1.4–2.0 s on a mid-size
   project (F4). A crashed daemon reports exit 4 after the full timeout rather
   than exit 3 at once (F3).

Limit 2 is the one that can damage state. Limits 1 and 3 cost correctness of
an answer and time, not data.

---

## Appendix — reproducing the measurements

Scripts are not committed. Each begins with `tyo3.require_release_build()`.
The project under test is a copy of `src/`, `tests/`, and `fixtures/` into a
temp directory, with `.tyo3/` removed before each run.

| Ref | What it measures |
|---|---|
| M1 | Entity population against the symbol cap; `locate` cost |
| M2 | Per-handler warm cost; the client's `context --id` path in and out of the cap |
| M3 | Whether the root lock is keyed on the root or the socket path |
| M4 | Decomposition of `snap.graph()`; the real agent loop |
| M5 | Late-response retention after a timeout |
| M6 | Transport-loss classification against a stub daemon |
| M7 | CLI per-invocation wall cost against a resident daemon |
| M8 | Shop-fixture size; read cost over an already-built graph |
