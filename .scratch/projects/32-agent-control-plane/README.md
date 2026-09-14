# Project 32 — agent-facing control plane

**Status: kickoff prepared; investigation not started.**

This project will investigate the surfaces TyO3 exposes to coding agents: the
Python session API, daemon, delta bus, native core, and Neovim integration. Its
goal is to describe the current control plane precisely before proposing any
new agent API, lifecycle rule, recovery behavior, or orchestration boundary.

Start with [KICKOFF.md](KICKOFF.md). It is an investigation-first brief for a
clean session and requires evidence from current source, tests, and focused
probes. The investigation must keep external agent-facing control distinct
from TyO3's internal coordination control plane.

Project 31 is complete and is not being reopened: it deliberately introduced
no `SemanticState` type. Its current behavior and the remaining conditional
time-travel performance candidate are recorded in
[Project 31's overview](../31-semantic-state/README.md).

Expected deliverables are a source-anchored investigation, a compact map of
agent-relevant capabilities and failure modes, and at most three evidence-led
follow-up projects. No implementation belongs in this project until a later
project is explicitly approved.
