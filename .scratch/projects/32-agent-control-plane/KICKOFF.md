# Project 32 — headless agent MVP kickoff

This is the implementation handoff for the narrowed Project 32 decision. The
goal is to make TyO3 useful to an external coding agent without Neovim. Keep
the implementation deliberately small: build a thin Python client over the
existing daemon, add a compact capability manifest, add revision metadata to
the core read responses, and reject stale mutations with an
`expected_revision` check.

Do not turn this into a general control-plane redesign. The broad
investigation and its evidence are preserved in the prior published history;
the current [README](README.md) and [INVESTIGATION](INVESTIGATION.md) define the
selected MVP.

## Objective

Enable this headless loop:

```text
agent connects to TyO3
  → learns root, session, revision, and available operations
  → asks for context/symbols/navigation/diagnostics
  → edits files with its normal filesystem tools or intentionally stages overlay text
  → synchronizes or reindexes TyO3
  → checks diagnostics and diff
  → retries only after re-reading when a revision is stale
```

“Agent-only” means no Neovim dependency. It does not mean TyO3 becomes the
filesystem writer. `sync_buffers` is an in-memory semantic overlay operation;
the agent owns durable working-tree edits in this MVP.

## Required reading

Read these in order before changing code:

1. `AGENTS.md` if present, then `README.md`, `CONTRIBUTING.md`, and `pyproject.toml`.
2. [Project 31 overview](../31-semantic-state/README.md) and
   [Project 31 investigation](../31-semantic-state/INVESTIGATION.md).
3. Current daemon protocol, actor, server, and handlers:
   `src/tyo3/daemon/protocol.py`, `session_actor.py`, `server.py`, `handlers.py`.
4. Current Python session/read APIs:
   `src/tyo3/session/session.py`, `read_ops.py`, and `views.py`.
5. Native project methods/commit path:
   `rust/src/project.rs`, `rust/src/project/methods.rs`, and
   `rust/src/project/commit.rs`.
6. Existing daemon tests:
   `tests/daemon/test_protocol.py`, `test_handlers.py`, and
   `test_end_to_end.py`.

Current source is authoritative if documentation disagrees.

## Existing capabilities to reuse

The implementation should reuse these existing routes rather than introduce a
second semantic API:

| MVP need | Existing route |
|---|---|
| Open/status | `ping`, `open` |
| Context | `context_pack`, `entity_at` |
| Symbol search | `symbols` |
| Navigation | `definition`, `references`, `hover`, `type_hierarchy`, `call_hierarchy` |
| Diagnostics | `check`, `diagnostics_at` |
| Semantic overlay update | `sync_buffer`, `sync_buffers` |
| Authored annotation | `author`, `authored` |
| Verification | `diff`, `ping` revision |

The native and Python layers already provide durable IDs, snapshots, atomic
multi-file commits, rollback, and graph projections. Do not duplicate those
concepts in the client.

## In-scope implementation

### 1. Headless Python client

Provide a small public client or CLI that external agents can use without
importing private daemon/session classes. It should handle:

- daemon connect/start and socket lifecycle;
- newline-delimited JSON-RPC framing and request correlation;
- capability parsing;
- high-level calls for context, search, navigation, checks, sync, author, and diff;
- typed errors for stale revision, closed session, evicted revision, and engine failure;
- status and post-mutation verification helpers.

Keep the client synchronous and simple unless existing project conventions
require otherwise. Do not add MCP in this project.

### 2. Capabilities manifest

Add an additive `protocol_version` and compact `capabilities` object to
`ping` or `open`. It should identify the core read/mutation operations and
important bounds such as the symbol result cap. It does not need a generated
schema registry.

The manifest must be tested against the actual advertised handler surface so
it cannot silently drift.

### 3. Revision-bearing reads

Add an observed `revision` field to the MVP responses for:

- `context_pack`;
- `entity_at`;
- `symbols`;
- `definition`;
- `references`;
- `hover`;
- `check`;
- `diagnostics_at`.

Preserve existing payload fields. The field means the native/session revision
observed for that result; it does not promise that separate calls share one
revision.

### 4. Stale mutation guard

Accept optional `expected_revision` on `sync_buffers` and `author`.

The comparison must occur inside one `SessionActor` work item immediately
before the native/session mutation. If the current revision differs:

- return a typed stale-revision error;
- include the current revision;
- publish no new revision or commit delta;
- do not partially apply the request.

On success, return the existing commit delta with additive `base_revision` and
`committed_revision` fields if needed. Preserve native publish-last and rollback
behavior.

### 5. Working-tree behavior

Document and test both explicit modes:

- working-tree mode: the agent edits files, then asks TyO3 to reindex/sync;
- overlay mode: the agent sends `sync_buffers` for unsaved semantic text.

Do not add a daemon filesystem-write operation. Resolve whether existing
`reindex` is sufficient or whether a narrow `sync_path` route is needed, but
keep that choice minimal and explicit.

## Deliberately out of scope

Do not implement:

- a `SemanticState` type or changes to Project 31 ownership;
- server-side snapshot handles or a time-travel service;
- event sequence numbers, replay, resume cursors, or durable subscriptions;
- mutation idempotency storage or cancellation guarantees;
- plan/apply workflows or broad explainability/provenance;
- multi-agent leases, authentication, or authorization;
- MCP or another external tool protocol;
- daemon-owned filesystem writes;
- broad graph/dependency API expansion;
- changes to GIL-release, identity, snapshot isolation, rollback, or existing
  Neovim behavior unless an additive compatibility fix is unavoidable.

For the MVP, notifications are optional hints. The client must use `ping`,
re-read, `check`, and `diff` for correctness after reconnect or a mutation.

## Acceptance tests

Add focused tests, preferably in the existing daemon test structure:

1. A headless client starts/connects to a daemon without Neovim.
2. `open`/`ping` returns protocol version, capabilities, root, session ID, and revision.
3. Core read calls return their observed revision without losing existing fields.
4. A multi-file `sync_buffers` request is one revision and returns a complete delta.
5. A stale `expected_revision` is rejected with no revision/delta/event.
6. Authored writes enforce the same stale check.
7. `ping` and `diff` verify a successful mutation.
8. Disconnect/reconnect followed by status and re-read converges without event replay.
9. `sync_buffers` is proven not to write source files.
10. Existing rollback, MVCC, bus, daemon, and Neovim tests remain green.
11. A client timeout is treated as an unknown outcome, not cancellation.
12. Capability metadata and advertised methods stay consistent.

The most important test is the stale-write race: advance the daemon revision
between an agent read and its mutation, then assert the old mutation is
rejected without a second commit.

## Verification and repository workflow

Run repository commands inside `devenv shell`, and put a reason on every shell
invocation:

```text
SECRETSPEC_REASON="Project 32 MVP check-rust" devenv shell -- check-rust
SECRETSPEC_REASON="Project 32 MVP clippy" devenv shell -- clippy
SECRETSPEC_REASON="Project 32 MVP parity oracle" devenv shell -- parity-oracle
SECRETSPEC_REASON="Project 32 MVP tests" devenv shell -- tests
```

Filter ambient `mypi`, `MYPI`, and `⚠️` setup noise from captured evidence; do
not treat it as a project test result.

Before every save, inspect the complete worktree with:

```text
SECRETSPEC_REASON="Project 32 MVP pre-save status" devenv shell -- gitman status
```

Route all version-control actions through `gitman`; never invoke raw `git` or
`jj`. Save and publish completed work promptly, and do not include unrelated
lanes.

## Definition of done

The MVP is done when an external agent can, without Neovim:

1. connect to a project and discover the supported operations;
2. obtain useful semantic context and diagnostics with an observed revision;
3. submit a guarded multi-file semantic update or authored write;
4. receive an unambiguous success/stale result;
5. verify the resulting revision and diff; and
6. recover from a reconnect by reopening and re-reading.

Anything beyond that belongs in a separately approved follow-up project.
