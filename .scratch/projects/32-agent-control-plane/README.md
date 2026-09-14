# Project 32 — agent-only MVP

**Status: MVP scope locked; implementation not started.**

The broad investigation is complete and has been deliberately narrowed. TyO3
already has the native semantic engine, Python session, daemon, atomic
multi-file overlay edits, diagnostics, durable IDs, and useful semantic query
handlers needed for an effective headless agent loop. The MVP is a thin agent
client over the existing daemon plus three small contract improvements:
capabilities in `ping`/`open`, revision-bearing MVP reads, and
`expected_revision` on mutation. It does not add a new control-plane state
object, snapshot service, event log, MCP server, or filesystem writer.

Read [INVESTIGATION.md](INVESTIGATION.md) for the evidence, exact boundary,
wire sketch, implementation tasks, and acceptance tests. Read
[KICKOFF.md](KICKOFF.md) for a self-contained handoff to the implementation
project.

The previous broad control-plane investigation is preserved in the preceding
published history point. This rewrite records the selected MVP rather than
discarding that evidence.

**Next decision:** start a separate implementation project for the headless
agent client and the three protocol hardenings. This project remains
documentation-only.
