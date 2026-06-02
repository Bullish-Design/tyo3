# Allium — Generate Implementation Guide

Generate a phased implementation guide from the Allium specs in `{{ spec_output_dir }}/` for `{{ repo_name }}`.

## Input

The specs describe `{{ repo_name }}`'s behavioural contracts. Use them to derive what must be built.

## Output format

Produce a guide with numbered phases. Each phase should be independently implementable and verifiable.

```markdown
# Implementation Guide: {{ repo_name }}

## Phase 1: <name>
- **Specs covered:** <list>
- **What to build:** <description>
- **Acceptance criteria:** <how to verify this phase is done>
- **Depends on:** <previous phases or external requirements>

## Phase 2: <name>
...
```

## Phase design principles

1. **Each phase maps to one or more rules/invariants** — don't split a single rule across phases
2. **Earlier phases build foundations** — entities and core surfaces come before rules that depend on them
3. **Phases are independently verifiable** — you can run checks after each phase and confirm progress
4. **Dependencies are explicit** — if Phase 4 needs Phase 2, state it
5. **Implementation detail stays in the guide** — the specs are the contract; the guide fills in *how*

## Spec-to-implementation mapping

For each rule, identify:
- **Rendering target** — Nix module? Python CLI? Shell script? Config file?
- **Key decision** — the one architectural choice that most affects correctness
- **Test strategy** — how to verify this specific rule holds

If the specs reference entities without implementation guidance (normal), use the `@guidance` annotations to inform the guide. If no guidance exists, propose a reasonable implementation approach and flag it as `[NEEDS DECISION]`.

## Reference skills

- `tend` — if you need to clarify specs before building the guide
- `weed` — if you need to check spec-code alignment during guide creation
