# Start prompt — build the TyO3 Neovim plugin (paste into a fresh session)

> Paste everything in the fenced block below as the first message of a new
> session, run from the `tyo3` repo root. It points at the two design/execution
> docs in this directory, which contain all the detail.
>
> **Base branch:** `main`. As of the post-`v0.2.0` fast-forward, `main` carries
> the kickoff docs, the design overview, and the `tyo3-demo tour`, so a fresh
> session can branch off `main` and read its own instructions.

---

```
Build the complete, production-usable TyO3 Neovim integration on a new branch.

Start here: read these two files in full before writing anything — they are the
source of truth (design, the exact TyO3 API to wrap, daemon + plugin specs,
testing/acceptance, and a step-by-step build sequence):
- .scratch/projects/20-neovim-integration/KICKOFF.md   (execution guide — the how)
- .scratch/projects/20-neovim-integration/OVERVIEW.md   (design — the what/why)
Also read the auto-loaded project memory and CLAUDE.md for devenv/house-rule
conventions.

What to build — the full thing, not an MVP:
1. tyo3d, a Python daemon under src/tyo3/daemon/ (new `tyo3-daemon` entry point)
   that wraps one TyO3Session, serves JSON-RPC over a unix socket, and pumps the
   delta bus to clients as notifications;
2. tyo3.nvim, the Lua plugin under editors/tyo3.nvim/ with buffer<->overlay sync,
   durable-id-anchored virtual-text notes/summaries, a floating entity inspector,
   an affected-set panel, Telescope pickers, commands, and :checkhealth.

Constraints (from KICKOFF sections 1 and 7):
- Create branch `nvim-plugin` off `main` (which contains these kickoff docs and
  the released v0.2.0 engine). Everything through `devenv shell --`. No AI
  attribution in commits/docs.
- Daemon-only — do NOT change the Rust/Python engine. It is complete; you are
  wrapping it. Learn the API from src/tyo3/demo/tour.py and
  src/tyo3/tests/test_final_acceptance.py (canonical, tested usage) plus the
  src/tyo3/session/ and src/tyo3/bus/ source — don't invent signatures. Mirror
  src/tyo3/precision/refiner.py for the bus-consumer threading pattern.
- Automated pytest for the daemon is the verifiable core (handlers + socket
  end-to-end); the Lua side gets headless specs where practical plus a manual
  checklist in the README.
- Follow the 7-step committable sequence in KICKOFF section 8. Ship the canonical
  demo: a scripted, recorded real-Neovim session (terminal nvim driven by vhs →
  tour.gif + asciinema tour.cast, regenerable headlessly), per
  .scratch/projects/20-neovim-integration/DEMO_RECORDING.md, embedded in the
  plugin README. When done: ruff clean, daemon pytest green, the v0.2.0 gate still
  green, write a PROGRESS.md in the project-20 directory, push the branch, and open
  a PR to `main` (do NOT merge) with the manual verification checklist in the body.

Work autonomously through the whole sequence; only stop to ask if you hit a
genuine design fork the docs don't resolve.
```

---

## Note on the PR base

`main` was fast-forwarded to include the post-`v0.2.0` doc/tour commits (projects
18–20 + `tyo3-demo tour`), so branching `nvim-plugin` off `main` is clean: a PR
from `nvim-plugin` → `main` will contain **only** the plugin work.
