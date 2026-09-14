# Project 32 — Agent-facing control plane: investigation

You are starting a clean investigation session in TyO3:
`/home/andrew/Documents/Projects/tyo3`.

TyO3 is a Python semantic engine built on Astral `ty` + Salsa, with a Rust/PyO3
core, durable code identity, a native code graph layer, layered annotations,
a delta bus, a daemon, and a Neovim plugin.

This is an investigation and architecture report first. Do not implement a
new control plane during this session. Do not redesign Project 31’s semantic
state decision. Establish evidence, identify the real control-plane boundary,
and produce a decision-ready recommendation. Any implementation should be a
separate, explicitly approved project after this report.

## Primary question

What should the agent-facing control plane of TyO3 be?

Here “agent-facing” means an external coding agent, LLM-driven tool, or
automation process that wants to understand and act on a live TyO3 project. Do
not assume that the current daemon API, Python session API, delta bus, or
Neovim integration is already the correct control plane. Determine what exists,
what is actually usable, where the boundaries are, and what is missing.

The investigation must cover both:

1. The control plane TyO3 exposes to an external agent.
2. The control plane TyO3 itself needs internally to coordinate sessions,
   snapshots, edits, semantic reads, annotations, subscriptions, and recovery.

Keep those two meanings distinct throughout the report.

## Read first, in order

1. Root repository guidance and overview:
   - `AGENTS.md`
   - `README.md`
   - `pyproject.toml`
   - relevant root-level architecture/development documentation
2. Project history and decisions:
   - `.scratch/projects/29-semantic-plane-cleanup/`
   - `.scratch/projects/30-snapshot-layer-cache/`
   - `.scratch/projects/31-semantic-state/README.md`
   - `.scratch/projects/31-semantic-state/INVESTIGATION.md`
   - `.scratch/projects/31-semantic-state/IMPLEMENTATION.md`
3. Python-facing surfaces:
   - `src/tyo3/session/`
   - `src/tyo3/daemon/`
   - `src/tyo3/graph/`
   - `src/tyo3/layers/`
   - `src/tyo3/models/`
   - `src/tyo3/bus/`
4. Native/Rust-facing surfaces:
   - `rust/src/project.rs`
   - `rust/src/project/`
   - `rust/src/code_layer.rs`
   - `rust/src/dto/`
   - `rust/src/content.rs`
   - `rust/src/identity.rs`
   - relevant bus, snapshot, authored, and overlay modules
5. Editor integration:
   - `editors/tyo3.nvim/`
   - its protocol/client/session code
   - demo and integration documentation where it describes actual behavior

Use current source as authoritative when documents disagree. Label historical
claims as historical. Project 29’s “lazily produced layer” wording is
superseded. Project 31’s conclusion is still settled: there is no
`SemanticState` type.

## Operating rules

- Run repository commands inside `devenv shell`.
- Every `devenv shell` command needs `SECRETSPEC_REASON="..."`.
- Filter ambient `mypi`, `MYPI`, and `⚠️` noise when capturing evidence.
- Route all version-control actions through `gitman`; never use raw `git` or `jj`.
- Before any save, inspect the complete worktree scope with `gitman status`.
- Do not touch the unrelated open lanes.
- Do not modify Rust/Python behavior during the investigation unless a tiny,
  isolated probe is necessary and it is kept under the new scratch project.
- Do not change public wire shapes, GIL-release boundaries, identity semantics,
  snapshot isolation, rollback guarantees, or the Python projection as part of
  this investigation.
- Do not turn an architectural hypothesis into a stated invariant.
- For every important claim, record `file:line` evidence, a reproducible
  command, or label it explicitly as an inference or unknown.
- Never quote a memory figure without reading the confound note in
  `31-semantic-state/INVESTIGATION.md §14.2`.

## Establish the baseline

Before drawing conclusions:

1. Inspect repository/lane status with `gitman status`.
2. Confirm the current trunk revision.
3. Run the smallest relevant health gates:
   - `devenv shell -- check-rust`
   - `devenv shell -- clippy`
   - `devenv shell -- parity-oracle`
   - `devenv shell -- tests`
4. Record actual counts and any ambient warnings separately from failures.
5. If the baseline differs from the documented state, stop and characterize the
   difference before relying on stale line references.

## Trace the real control plane

Build an evidence-backed end-to-end map for each path below.

### A. Agent/session lifecycle

Trace:

- project discovery and open
- initial content ingestion
- identity reconciliation
- initial code-layer materialization
- session ownership and close/reload
- long-lived session behavior
- daemon session creation, lookup, expiry, and shutdown
- whether multiple clients/agents can share a session
- what identifies a project, session, revision, snapshot, and actor

Identify which objects are authoritative, which are projections, and which
operations are state-changing versus read-only.

### B. Agent read/query surface

Inventory the actual callable operations an agent can use:

- files and project metadata
- diagnostics and checks
- symbols/entities
- code graph nodes and edges
- references, dependencies, hierarchy, occurrences, and paths
- snapshots and time travel
- identity/durable IDs
- authored annotations and derived layers
- status/revision/change information

For each operation, record:

- Python API, daemon/API endpoint, or Neovim route
- input and output shape
- revision semantics
- blocking/async behavior
- GIL/thread behavior where relevant
- error behavior
- whether the result is deterministic and revision-pinned
- whether the operation is suitable for an autonomous agent

### C. Agent write/action surface

Trace:

- edit and virtual-edit operations
- sync/watch paths
- commit boundaries and failure/rollback behavior
- code deltas and bus publication
- authored annotation writes
- review/accept/reject or equivalent workflows
- actions available through the daemon and Neovim
- whether an agent can request a dry run, preview, explainability data, or
  confirmation before mutation

Separate semantic edits from filesystem edits and from annotation writes.
Record what an agent can mutate accidentally and what guardrails exist.

### D. Eventing and subscriptions

Trace the delta bus and all consumers:

- event types
- revision ordering
- filtering/interest mechanisms
- backpressure and overflow policy
- reconnect/resume behavior
- whether events are replayable
- snapshot/event consistency
- daemon-to-client and native-to-Python boundaries
- what a long-lived agent must do after missed or out-of-order events

Determine whether the event surface is a usable control-plane protocol or only
an internal notification mechanism.

### E. Concurrency, isolation, and recovery

Investigate:

- concurrent reads and writes
- held snapshots and writer behavior
- daemon session concurrency
- cancellation
- stale clients
- revision eviction
- process restart and reopen
- malformed input and partial failure
- sidecar/identity persistence failures
- whether an agent can recover from every documented failure without
  reinitializing the whole project

Pay particular attention to the distinction between:

- live floating HEAD
- revision-pinned snapshots
- time-travel snapshots
- Python projections/caches
- native `Arc<CodeLayer>` ownership
- persisted identity/authored data
- bus/event state

## Assess the control-plane qualities

Evaluate the current and possible control plane against these qualities:

- discoverability: can a new agent learn the API from machine-readable metadata?
- capability negotiation: can clients discover supported operations and versions?
- explicitness: are revisions, sessions, actors, and mutations unambiguous?
- determinism: can an agent reproduce and verify a result?
- safety: are destructive or broad mutations guarded?
- provenance: can the agent tell why a result or diagnostic exists?
- composability: can read/query/action operations be composed without hidden
  side effects?
- observability: can an agent inspect health, lag, queue state, and revision?
- resumability: can it reconnect without losing semantic state?
- idempotence: can retries safely repeat operations?
- boundedness: are work, memory, event queues, and retention bounded?
- explainability: can the system expose affected files/symbols and reasons?
- editor integration: do daemon, Python, and Neovim surfaces agree?
- testability: can the protocol and invariants be tested without a live editor?
- security/trust boundaries: what can an external agent read or mutate?

Do not score these abstractly without evidence. For each weakness, show the
concrete path and the user/agent consequence.

## Required outputs

Create a new scratch project:
`.scratch/projects/32-agent-control-plane/`

Produce:

1. `README.md`
   - one-paragraph outcome
   - scope and non-goals
   - links to the report and any probes
   - current status and next decision
2. `INVESTIGATION.md`
   - executive summary
   - terminology and actors
   - current architecture/data-flow diagram
   - lifecycle/state-machine diagram
   - operation inventory tables
   - daemon/Python/Neovim/native surface mapping
   - ownership and revision-consistency table
   - event/delta/bus analysis
   - concurrency/failure/recovery analysis
   - evidence ledger with `file:line` anchors
   - gaps and opportunities, prioritized by impact and cost
   - alternatives considered
   - explicit non-goals and rejected assumptions
   - test/probe obligations
   - risks and open questions
   - recommendation with a clearly stated decision boundary
3. `KICKOFF.md`
   - a self-contained version of the final investigation instructions so the
     work can be resumed by another agent
4. Any probe scripts only if they add repeatable evidence. Keep them small,
   documented, and do not commit generated output.

Distinguish every report statement as one of:

- verified fact
- measured result
- inference
- recommendation
- unresolved question

Use exact source anchors for verified facts. Do not claim “the control plane”
is one object unless the evidence supports that conclusion.

## Decision discipline

Do not implement merely because an API is awkward. First decide whether the
problem is:

- missing protocol surface,
- inconsistent existing surfaces,
- lifecycle/ownership ambiguity,
- lack of machine-readable capability metadata,
- event/recovery weakness,
- semantic correctness issue,
- performance/memory issue,
- editor-only ergonomics,
- or documentation/discoverability.

Do not introduce a wrapper state type as a naming exercise. Preserve Project 31’s
settled conclusion unless new evidence directly overturns one of its three
findings; if that happens, stop and report the contradiction instead of
redesigning unilaterally.

At the end, recommend at most three next projects. For each, state:

- exact user/agent problem
- evidence
- proposed boundary
- expected benefit
- costs and risks
- why it should be done now or deferred
- minimum acceptance tests

The likely output is an investigation and a prioritized roadmap, not code.
