---
name: allium-entrypoint
description: Start here for all Allium work. Routes to the right skill based on your task and tells you where specs live in this project.
auto_trigger:
  - keywords: ["allium", "allium spec", "allium specification", ".allium file", "behavioural spec", "behavioral spec"]
---

# Allium Entrypoint

Allium is a formal language for capturing software behaviour at the domain level. It sits between informal feature descriptions and implementation, providing a precise way to specify what software does without prescribing how it's built.

This project has Allium skills installed. Use this skill to find the right one for your task.

## Specs directory

**All `.allium` files in this project live under: `.scratch/specs/allium`**

- Read specs from this directory when you need to understand existing behaviour.
- Write new specs to this directory.
- The entrypoint file is `.scratch/specs/allium/index.allium` (create it if it doesn't exist).
- Use `use "./other.allium" as other` for modular specs within the directory.

## Skill routing

### Writing and understanding specs (domain skills)

Use these when working directly with `.allium` files and behavioural specifications.

| Task | Skill | When to use |
|------|-------|-------------|
| Language syntax and structure | `allium` | Writing, reading, or reviewing `.allium` files |
| Build a spec from conversation | `elicit` | User describes a feature or behaviour they want |
| Extract a spec from code | `distill` | User has code and wants a spec from it |
| Modify an existing spec | `tend` | Targeted changes to `.allium` files |
| Check spec-code alignment | `weed` | Find or fix divergences between spec and code |
| Generate tests from a spec | `propagate` | Derive tests from a specification |

### Running the Allium CLI (tool skills)

Use these when you need to validate, analyse, or extract data from specs using the `allium` command-line tool.

| Task | Skill | When to use |
|------|-------|-------------|
| Choose the right CLI command | `allium-cli-overview` | Entry point for all CLI operations |
| Project folder conventions | `allium-cli-project-conventions` | Entrypoint resolution and folder layout |
| Validate specs | `allium-cli-check` | After editing any `.allium` file |
| Semantic analysis | `allium-cli-analyse` | Before implementation or test generation |
| Parse syntax tree | `allium-cli-parse` | Debug confusing syntax diagnostics |
| Extract domain model | `allium-cli-model` | Get entities, rules, transitions as data |
| Derive test plan | `allium-cli-plan` | Generate test obligations from a spec |
| Fix CLI failures | `allium-cli-diagnostics-repair` | When a CLI command fails |
| Command syntax reference | `allium-cli-command-reference` | Quick lookup of command flags and options |
| CI and quality gates | `allium-cli-ci-and-quality-gates` | Integrate validation into CI pipelines |
| Output interpretation | `allium-cli-output-interpretation` | Understand CLI output formats |
| Agent runbooks | `allium-cli-agent-runbooks` | End-to-end workflows combining multiple commands |

## Quick decision guide

1. **"I want to create or edit a spec"** — use a domain skill (`elicit` to start from scratch, `tend` to modify, `allium` for syntax reference)
2. **"I want to validate a spec"** — use `allium-cli-check` (always run after editing)
3. **"I want to understand what a spec says"** — read the `.allium` files in `.scratch/specs/allium`, use the `allium` skill for syntax help
4. **"I want to generate tests"** — use `propagate` for test design, `allium-cli-plan` for CLI-derived test obligations
5. **"I want to check if code matches the spec"** — use `weed`
6. **"I want to extract a spec from existing code"** — use `distill`

## Mandatory validation

After any `.allium` edit, always run validation:

```bash
allium check ".scratch/specs/allium/index.allium"
```

Before implementation or test generation, also run:

```bash
allium analyse ".scratch/specs/allium/index.allium"
```
