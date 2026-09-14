# Project 31 — closeout and current handoff

Project 31 is complete. This file is retained as the entrypoint for agents
auditing the decision or its implementation; it is no longer an implementation
prompt.

## Read first

1. [README.md](README.md) — current status, behavior, and next work.
2. [INVESTIGATION.md](INVESTIGATION.md) — evidence and the settled decision.
3. [IMPLEMENTATION.md](IMPLEMENTATION.md) — historical execution guide and
   verification record.
4. [SPIKES.md](SPIKES.md) — pre-implementation premise, graph, empty-project,
   helper-location, and timing-noise checks.

Use current source as authoritative when any historical line anchor differs.

## Settled decision

There is no `SemanticState` type. The registry and code layer are not one
revision-shaped value:

- `IdentityRegistry` is a persisted, deep-cloned, last-known identity record
  that survives reload and retains orphaned anchors.
- `CodeLayer` is an unpersisted, `Arc`-shared, current-revision semantic layer
  derived from the reconciled registry. It includes synthetic module nodes and
  external stubs.
- During reconciliation the registry must advance before the next layer can be
  produced. A wrapper would not make that sequence atomic and would misstate the
  intermediate state.
- An owned duplicate would violate the measured memory constraint.

Do not reopen this design question without new evidence that directly overturns
one of those findings.

## Landed implementation

- The duplicated full-code-delta body is one `full_code_delta_for` helper in
  `rust/src/project.rs`; both head and snapshot callers retain their existing
  `py.detach` boundaries.
- The empty-layer miss rule is one `HeadState::servable_code_layer` helper.
  The snapshot site retains its explicit `is_head` guard.
- Non-empty projects materialize the initial code layer during `open()`, after
  identity reconciliation. This is the long-lived-session optimization: the
  full build is paid once at startup, and repeated reads use the carried layer.
- Empty projects still use the empty-layer fallback. Time-travel snapshots
  still rebuild because the head layer is not valid for an older revision.
- `TyProjectState.code_layer` remains `Option<Arc<CodeLayer>>`,
  `HeadState.code_layer` remains `Arc<CodeLayer>`, and the wire, GIL, Python
  projection, and rollback contracts are unchanged.

## Verification

The landed implementation was verified with:

- 174 Rust tests passed.
- 836 Python tests passed, with 4 deselected.
- parity oracle: 8/8 passed.
- `check-rust`, clippy, and native rebuild passed.
- repository-scale repeated full-delta reads: approximately 0.17–0.19 s.
- repository-scale `open()`: approximately 4.69 s.

See INVESTIGATION §14.5 for the measurement table and memory caveat.

## Next investigation

Do not start another semantic-state refactor. The next planned investigation is
the agent-facing control plane in
`.scratch/projects/32-agent-control-plane/KICKOFF.md`. It should map the
current Python, daemon, delta-bus, native, and Neovim surfaces before proposing
new agent APIs, lifecycle rules, or recovery semantics.

The only remaining Project 31 performance candidate is bounded code-layer
retention for time-travel snapshots. Investigate real usage first; it costs
approximately 12.83 MiB per retained layer and needs an eviction policy.
