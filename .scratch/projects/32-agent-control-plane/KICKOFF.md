# Project 32 — headless agent MVP kickoff (revised)

**Status:** scope revised from measurement. Implementation not started.
**Revised:** 2026-09-14

This document replaces the earlier kickoff. The earlier scope was written
before anyone measured the engine. The measurements changed three decisions.
Read [MEASUREMENTS](#measured-evidence) before you argue with the scope.

Read [IMPLEMENTATION.md](IMPLEMENTATION.md) for the ordered build steps.

## What changed, and why

The earlier plan built a capability manifest, revision-bearing reads, and an
`expected_revision` stale-mutation guard, then wrapped them in a Python client.
Three findings moved the scope:

1. **The engine already answers the agent's hard question, and the daemon does
   not expose it.** `graph.transitive_dependents` computes blast radius. No
   daemon route reaches it. The earlier client surface was built almost
   entirely from commodity verbs (search, navigate, check) that an agent
   already approximates with ripgrep and `ty check`.
2. **`reindex` costs a flat ~1 s and returns a precise id-level delta.** Disk
   synchronisation needs no new engine work. The overlay path (`sync_buffers`)
   is not faster and it creates a divergence hazard. Agents must not use it.
3. **The stale-mutation guard protects a path the agent must not take.** An
   agent writes source files with its own tools. It does not mutate code
   through the daemon. Guarding `sync_buffers` guards nothing the agent does.

## Objective

Enable this loop, with no Neovim and no TyO3 imports:

```text
agent connects to a project
  -> asks what will break if it changes an entity      (impact)
  -> reads the entity and its context                  (context)
  -> edits files with its own filesystem tools
  -> tells TyO3 to re-read the working tree            (sync)
  -> checks diagnostics and what changed               (check, changed)
  -> records durable notes against entity identity     (note)
```

## The contract

These three rules are the design. Test them, document them, do not soften them.

1. **The agent owns the filesystem.** It writes source files with its own
   tools.
2. **TyO3 never writes source files.** No daemon filesystem-write route exists
   or gets added.
3. **The agent never sends overlay text.** `sync_buffer` and `sync_buffers`
   are the Neovim unsaved-buffer path. The agent uses `sync` instead. This
   rule removes the overlay/working-tree divergence hazard completely.

## Measured evidence

All numbers come from a **release** build. The repository is 167 files with
real dependencies (pydantic, rustworkx). Reproduce with the scripts named in
[IMPLEMENTATION.md](IMPLEMENTATION.md) Step 0.

| Operation | Cost | Delta quality |
|---|---|---|
| `open` (daemon start) | ~1.7 s | one-time |
| `edit` / `sync_path` / `reindex`, any change size | **~1.0 s** | precise (`created=1`) |
| `edit_many`, 5 files | ~2.5 s | precise |
| `check` | 299 ms cold, **11 ms cached** | — |
| `diff` across revisions | ~2.7 s | precise |

Three conclusions follow:

- **The commit cost is native.** `_inner.edit()` alone takes 1029 ms; the full
  `session.edit()` takes 1070 ms. Python adds about 5%. No Python-side
  optimisation is available.
- **Cost tracks semantic complexity, not file count.** A 1600-file synthetic
  project commits in 222 ms. The 167-file real repository takes 1030 ms. The
  expense is resolving real dependency graphs.
- **`reindex` is flat-cost and precise.** It costs ~1 s whether one file or
  five changed, so it beats per-file synchronisation for any batch. It reports
  `created=1` for one added function on a clean registry.

The agent loop therefore costs about **1.3 s** of TyO3 overhead per iteration
(one `sync`, one `check`), with reads in milliseconds.

### Measurement hazards found

Two artefacts invalidated earlier readings. Both are now guarded in Step 0.

- **Build profile.** `devenv shell -- build` installs a debug build to the same
  path as `build-release`. Debug runs the commit path about 6x slower. Nothing
  reported which profile was loaded.
- **Identity registry pollution.** Benchmarks that add and remove entities
  leave orphaned records in `.tyo3/identity.db`. Those records resurrect on a
  later run and corrupt delta counts. Benchmark against a fresh project, or
  use entity names that have never existed.

## In scope

### 1. Build-profile guard

Export the Cargo profile from the native module and assert it where
performance matters. See [IMPLEMENTATION.md](IMPLEMENTATION.md) Step 0. Do this
first — it protects every later measurement.

### 2. The `impact` route

One new handler. Resolve the entity at a position or durable id, then return
its transitive dependents from `graph.transitive_dependents`, grouped by file,
each with its range and durable id. Cap the result and report truncation.

This is the differentiated verb. An agent can already find and read code; it
cannot cheaply learn what its change will break.

### 3. Headless client and CLI

Add `AgentClient`, a synchronous client over the existing Unix JSON-RPC socket.
It owns daemon start/connect, framing, request correlation, and typed errors.

Add a `tyo3-agent` CLI over that client, built with `typer-slim`. The CLI is a
thin shell. All logic lives in the client, so tests drive the client directly
and a later MCP adapter wraps the client rather than the CLI.

Commands:

| Command | Route | New work |
|---|---|---|
| `status` | `ping`, `open` | adds `instance_id`, `protocol_version` |
| `sync` | `reindex` | none |
| `find <query>` | `symbols` | adds `truncated`, `limit` |
| `context <target>` | `context_pack`, `entity_at` | none |
| `impact <target>` | new `impact` route | the one new handler |
| `check [path]` | `check` | none |
| `changed --since <rev>` | `diff` | none |
| `note` / `notes [--stale]` | `author`, `layer_ids`, `review_state` | none |

Output rules:

- Emit JSON on stdout by default when stdout is not a TTY. Emit a human table
  when it is. `--json` forces JSON either way.
- Write diagnostics to stderr. Never mix them into stdout.
- Use stable exit codes: 0 success, 1 engine error, 2 usage error,
  3 daemon unavailable, 4 conflict or stale revision.

### 4. Four protocol corrections

All four are additive. None changes an existing field's meaning.

- Add `revision` to the MVP read responses: `context_pack`, `entity_at`,
  `symbols`, `definition`, `references`, `hover`, `check`, `diagnostics_at`,
  and the new `impact`. Take the value from the pinned snapshot, not
  `session.head`.
- Add `error_type` to the JSON-RPC error `data` object, and add a `data` field
  to `ProtocolError`. Without this the client must parse prose to type an
  error.
- Add `instance_id`, a fresh UUID per daemon process, to `ping` and `open`.
  `session_id` is `sha1(root)` and stays constant across restarts, so an agent
  cannot currently detect that the daemon restarted and reset its revision
  counter.
- Add `truncated` and `limit` to the `symbols` response. It currently caps at
  500 before sorting and returns an arbitrary subset that looks complete.

### 5. Documentation

Document the contract, the two measurement hazards, and the agent loop with
its measured cost.

## Out of scope

Do not implement:

- `expected_revision` or any stale-mutation guard;
- a capability manifest (the CLI's `--help` is the manifest, and it cannot
  drift);
- an agent-facing overlay path, or a native `sync_paths` batch route;
- a daemon filesystem-write route;
- MCP or another tool protocol — but keep `AgentClient` adapter-ready;
- an ambient file watcher in the daemon;
- server-side snapshot handles, event sequence numbers, replay, or resume
  cursors;
- mutation idempotency or cancellation guarantees;
- a `SemanticState` type, or any change to Project 31 ownership;
- multi-agent leases, authentication, or authorization;
- changes to GIL release, identity, snapshot isolation, or rollback.

## Acceptance tests

1. The native module reports its build profile, and a release assertion fails
   on a debug build.
2. `AgentClient` starts or connects to a daemon with no Neovim.
3. `status` returns root, session id, instance id, protocol version, and
   revision. A daemon restart changes `instance_id`.
4. Every MVP read returns its observed revision, and keeps all existing
   fields.
5. `impact` returns the transitive dependents of a known entity, and reports
   truncation at the cap.
6. `sync` picks up a file the test wrote with plain filesystem calls, in one
   revision, with a precise id-level delta.
7. An engine error reaches the client as a typed error, with no string
   parsing.
8. `symbols` sets `truncated` when it caps.
9. A disconnect and reconnect converges through `status` and a re-read, with
   no event replay.
10. The CLI emits JSON when stdout is not a TTY, and exits with the documented
    codes.
11. No daemon route writes a source file.
12. Existing rollback, MVCC, bus, daemon, and Neovim tests stay green.

## Verification

Run every repository command inside `devenv shell`, with a reason:

```text
SECRETSPEC_REASON="Project 32 build release" devenv shell -- build-release
SECRETSPEC_REASON="Project 32 check-rust"    devenv shell -- check-rust
SECRETSPEC_REASON="Project 32 clippy"        devenv shell -- clippy
SECRETSPEC_REASON="Project 32 parity oracle" devenv shell -- parity-oracle
SECRETSPEC_REASON="Project 32 tests"         devenv shell -- tests
```

Filter ambient `mypi`, `MYPI`, and warning-sign setup noise from captured
evidence. It is not a project test result.

Measure only on a release build. `devenv shell -- build` installs a debug
build to the same path.

Inspect the whole worktree before every save:

```text
SECRETSPEC_REASON="Project 32 pre-save status" devenv shell -- gitman status
```

Route all version control through `gitman`. Never call `git` or `jj` directly.

## Definition of done

An external agent can, with no Neovim and no TyO3 import:

1. connect to a project and read its status;
2. find an entity and learn what its change would break;
3. read that entity's context with an observed revision;
4. edit files with its own tools, then synchronise TyO3 in one call;
5. check diagnostics and inspect the precise id-level change;
6. record a durable note that survives a later refactor; and
7. recover from a reconnect by re-reading.
