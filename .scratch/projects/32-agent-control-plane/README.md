# Project 32 — agent-only MVP

**Status: scope revised from measurement (2026-09-14). Implementation not started.**

TyO3 already has the native semantic engine, Python session, daemon, durable
identity, diagnostics, and graph queries an external agent needs. The MVP adds
one differentiated route, a client, and a CLI — not a new control plane.

Read [KICKOFF.md](KICKOFF.md) for the scope and the measured evidence behind it.
Read [IMPLEMENTATION.md](IMPLEMENTATION.md) for the ordered build steps.

## The contract

1. The agent owns the filesystem.
2. TyO3 never writes source files.
3. The agent never sends overlay text. `sync_buffers` is the Neovim
   unsaved-buffer path.

## What the MVP builds

- A `tyo3-agent` CLI over a synchronous `AgentClient`.
- One new daemon route, `impact` — the transitive dependents of an entity.
  This is the question an agent cannot answer with ripgrep.
- Four additive protocol corrections: observed `revision` on the MVP reads,
  `error_type` in error payloads, `instance_id` for restart detection, and
  truncation reporting on `symbols`.
- A build-profile guard, because debug and release install to the same path.

## What changed from the first scope

[INVESTIGATION.md](INVESTIGATION.md) predates any measurement. Three of its
recommendations did not survive contact with the numbers:

- The `expected_revision` stale-mutation guard is dropped. It protects
  `sync_buffers`, which agents must not call.
- The capability manifest is dropped. The CLI's `--help` cannot drift.
- Agent-facing overlay mode is dropped. `reindex` costs a flat ~1 s and
  returns a precise id-level delta, so disk synchronisation needs no new
  engine work.

The investigation's evidence about the existing architecture remains accurate.
Its roadmap does not. KICKOFF.md is authoritative.
