---
name: allium-cli-analyse
description: Use when running deeper semantic and process-level analysis on a folder of .allium specs after allium check has passed.
---

# Allium CLI Analyse

Use this skill when the spec folder has passed `allium check` and the next task requires semantic confidence, process-level review, implementation readiness, or test-generation readiness.

## Primary objective

Run `allium analyse` against the resolved entrypoint for a folder of `.allium` specs and convert findings into concrete next actions.


## Folder-level operating protocol

This skill is invoked against a folder containing one or more `.allium` files. Do not pass the folder itself to `allium` commands. Allium CLI commands operate on spec files, so first resolve the effective entrypoint file.

### Resolve the spec folder

Treat the user's folder argument as `SPEC_DIR`. If no folder is provided, use the current working directory only after confirming it contains `.allium` files.

```bash
SPEC_DIR="${SPEC_DIR:-.}"
find "$SPEC_DIR" -type f -name '*.allium' | sort
```

If no `.allium` files are found, stop and report that the folder does not contain Allium specs.

### Resolve the entrypoint

Use this order:

1. If the user explicitly named an entrypoint file, use that exact file.
2. If `SPEC_DIR/index.allium` exists, use it.
3. If `SPEC_DIR/allium/index.allium` exists, use it.
4. If exactly one `.allium` file exists under `SPEC_DIR`, use that file.
5. If multiple `.allium` files exist and no clear entrypoint exists, inspect imports/references and choose the file that appears to aggregate the others.
6. If still ambiguous, run safe per-file diagnostics where useful, then report the candidate entrypoints and ask for a project convention. Do not invent a permanent convention.

Use `ENTRYPOINT` for commands that need the whole model. Use a leaf file only for narrow syntax debugging with `allium parse`, and return to the entrypoint for validation.

```bash
ENTRYPOINT="$SPEC_DIR/index.allium"
allium check "$ENTRYPOINT"
```

### Never do this

```bash
allium check "$SPEC_DIR"
allium analyse "$SPEC_DIR"
allium model "$SPEC_DIR"
allium plan "$SPEC_DIR"
```

Passing a directory produces misleading failures and does not validate the spec set.


## Preconditions

Run `check` first:

```bash
allium check "$ENTRYPOINT"
```

Only run `analyse` after `check` passes, unless the task is specifically to investigate why analysis behaves unexpectedly.

## Standard workflow

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

If `analyse` passes, the spec is a better candidate for implementation or test planning. If it fails, stop and repair the diagnostic before proceeding.

## What `analyse` is for

Use `analyse` to inspect issues that basic validation may not fully address:

- Process-level completeness.
- Data-flow availability across rules and surfaces.
- Reachability of rules or states.
- Dead-end lifecycles.
- Conflict detection.
- Deadlock-like process issues.
- Invariant verification.
- Transition consistency.
- State-dependent field coverage.

## When to run it

Run `analyse`:

- Before implementing code from a materially changed spec.
- Before generating tests from `allium plan`.
- Before claiming a spec is semantically clean.
- After lifecycle, transition, invariant, or cross-entity edits.
- When user asks for a deeper review of a spec folder.

## Interpreting common analysis findings

### Reachability problem

Meaning: some rule, state, or behavior cannot be reached from the modeled process.

Likely causes:

- Missing rule that enters a state.
- Preconditions that can never be satisfied.
- Broken transition graph.
- Missing surface/input that provides required data.

Repair strategy:

1. Locate the unreachable item.
2. Trace what rule should make it reachable.
3. Add or repair the missing transition, precondition, or data source.
4. Rerun `check`, then `analyse`.

### Dead-end state

Meaning: an entity can enter a state but cannot continue to a terminal or intended next state.

Likely causes:

- Missing terminal declaration.
- Missing outgoing transition.
- Missing cancellation/failure/retry path.
- Incorrectly declared terminal state.

Repair strategy:

1. Decide whether the state should be terminal.
2. If terminal, declare it as terminal.
3. If not terminal, add the intended outgoing transition and rule coverage.
4. Rerun `analyse`.

### Conflict detection

Meaning: two modeled behaviors may be inconsistent, overlapping, or mutually incompatible.

Likely causes:

- Rules with overlapping triggers and contradictory outcomes.
- Preconditions that do not separate cases.
- Invariants that conflict with rule outcomes.
- Lifecycle transitions that allow invalid combinations.

Repair strategy:

1. Identify the overlapping condition.
2. Decide whether behavior should be split, ordered, forbidden, or made explicit.
3. Tighten preconditions or outcomes.
4. Rerun `check`, then `analyse`.

### Invariant failure

Meaning: a rule or reachable state may violate an invariant.

Likely causes:

- Rule outcome does not preserve a numeric or relational constraint.
- Missing guard in `requires`.
- Invariant is too broad or attached to the wrong entity.

Repair strategy:

1. Verify the invariant expresses true domain intent.
2. Add required guards or outcomes.
3. Avoid weakening invariants unless the domain intent changed.
4. Rerun `analyse`.

### State-dependent field issue

Meaning: a rule transitions into a state that requires fields but does not set them.

Likely causes:

- Field has `when status = ...` requirement.
- Rule changes status but omits required fields.
- Transition split moved required data to another rule incorrectly.

Repair strategy:

1. Identify the target state.
2. Identify fields required in that state.
3. Update the transitioning rule to set those fields or change the lifecycle design.
4. Rerun `check`, then `analyse`.

## Folder-level examples

### Analyse canonical spec folder

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

### Analyse scratch spec folder

```bash
SPEC_DIR=".scratch/specs/allium"
ENTRYPOINT=".scratch/specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

## Success criteria

A successful `analyse` run means:

- Basic validation passed first.
- No blocking process-level or semantic diagnostics were reported by the CLI.
- The spec is suitable for the next step requested by the user, subject to the limits of the CLI.

It does not mean:

- The spec is complete relative to unstated user intent.
- The implementation matches the spec.
- Generated tests will be sufficient without review.

## Failure policy

When `analyse` fails:

1. Stop downstream commands.
2. Preserve exact diagnostic output.
3. Classify the finding.
4. Repair the smallest coherent spec region.
5. Run `allium check "$ENTRYPOINT"`.
6. Run `allium analyse "$ENTRYPOINT"` again.


## Reporting requirements

When reporting command results, include:

- The exact command run.
- Whether it passed or failed.
- The resolved spec folder.
- The resolved entrypoint.
- Blocking diagnostics, if any.
- The smallest meaningful next action.

Do not claim a spec is valid unless the relevant command completed successfully against the resolved entrypoint.


## Related skills

- `allium-cli-check` for required pre-validation.
- `allium-cli-diagnostics-repair` for failures.
- `allium-cli-plan` after successful analysis.
