# Allium — Elicit Initial Specification

You are building an Allium behavioural specification for `{{ repo_name }}`.

## Input

The user will provide a concept description. This may be:
- Plain text passed directly in the conversation
- A path to a concept document on disk

Read the concept carefully. If a file path is given, read it first.

## What is Allium?

Allium is a behavioural specification language. Specs describe **what a system does** — entities, rules, surfaces, and invariants — without prescribing how it is implemented. Specs live in `.allium` files and use a declarative syntax:

- **Entities** describe state: fields with types and conditional presence (`when`)
- **Surfaces** define trigger events and guarantees
- **Rules** connect triggers to state transitions (`when ... ensures ...`)
- **Invariants** express constraints that must always hold
- **`@guidance`** annotations capture implementation hints

## Your task

Run a structured discovery session. Load the **elicit** skill and follow its protocol.

At minimum, elicit:

1. **Core entities** — what stateful things exist? What fields define them?
2. **Surfaces** — what events enter the system? What guarantees must each surface uphold?
3. **Rules** — for each surface event, what must become true?
4. **Invariants** — what must never be violated?

Write the resulting spec to `{{ spec_output_dir }}/<name>.allium`.

## Reference skills

- `elicit` — structured discovery protocol
- `allium` — language reference (syntax, patterns, migration guides)
- `tend` — writing and editing specs

## Principles

- Model **behaviour**, not implementation
- No overspecification — entities carry only the fields rules need
- Surfaces enumerate events exhaustively; rules consume them
- Guidance is for implementors, not part of the specification logic
