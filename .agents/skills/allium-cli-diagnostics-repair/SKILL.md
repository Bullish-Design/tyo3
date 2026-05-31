---
name: allium-cli-diagnostics-repair
description: Use when an Allium CLI command fails on a folder of .allium specs. Classifies diagnostics, performs minimal repairs, and reruns the correct command against the resolved entrypoint.
---

# Allium CLI Diagnostics Repair

Use this skill when `allium check`, `allium analyse`, `allium parse`, `allium model`, or `allium plan` fails while operating on a folder of `.allium` specs.

## Primary objective

Repair CLI diagnostics systematically with the smallest behavior-preserving spec changes, then rerun the failing command against the correct file.


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


## Core repair loop

```text
1. Reproduce the failure with the exact command.
2. Capture the exact diagnostic.
3. Identify the command type: check, analyse, parse, model, or plan.
4. Identify the file/span or inferred source region.
5. Classify the diagnostic.
6. Patch the smallest coherent region.
7. Rerun the same command.
8. If it passes, run the next stronger command if appropriate.
9. If it fails differently, repeat from the new diagnostic.
```

Do not perform broad rewrites while fixing diagnostics. Do not silently reinterpret domain intent just to satisfy the CLI.

## Diagnostic classes and repair strategies

### `directory-passed-instead-of-file`

Symptoms:

- Output contains “Is a directory”.
- Command was run as `allium model specs/allium/` or similar.

Repair:

```bash
ENTRYPOINT="$SPEC_DIR/index.allium"
allium check "$ENTRYPOINT"
```

Then rerun the originally intended command against `ENTRYPOINT`.

### `wrong-entrypoint`

Symptoms:

- Command passes but output is unexpectedly sparse.
- Expected entities/rules are missing.
- Leaf file validates but folder-level behavior is absent.

Repair:

1. List all `.allium` files.
2. Find `index.allium` or aggregate file.
3. Verify imports/references connect the leaf files.
4. Run `allium check "$ENTRYPOINT"`.

### `syntax-error`

Symptoms:

- Parse/check failure near a declaration, brace, field, expression, or block.

Repair:

```bash
allium parse "$TARGET_FILE"
# patch smallest malformed syntax
allium parse "$TARGET_FILE"
allium check "$ENTRYPOINT"
```

Prefer fixing braces, clause placement, or local expression shape before editing domain semantics.

### `invalid-version-marker`

Symptoms:

- Missing or unsupported `-- allium: ...` marker.
- Diagnostics mention version or language level.

Repair:

1. Inspect the top of the file.
2. Restore the expected marker for the repository.
3. Do not migrate language versions unless explicitly requested.
4. Rerun `check`.

### `missing-import`

Symptoms:

- Entry point cannot see a file or module.
- Declarations exist but are invisible from the entrypoint.

Repair:

1. Confirm target file exists under `SPEC_DIR`.
2. Inspect current import/aggregation conventions.
3. Add the minimal import or reference needed.
4. Rerun `check` and, if relevant, `model`.

### `missing-reference`

Symptoms:

- Unknown entity, field, rule, state, enum value, surface, actor, or relationship.

Repair:

1. Search the spec folder for the intended symbol.
2. Decide whether the reference is misspelled or the declaration is missing.
3. Prefer correcting a typo over adding a duplicate declaration.
4. Verify visibility from `ENTRYPOINT`.
5. Rerun `check`.

### `invalid-transition`

Symptoms:

- Rule changes a status in a way not declared in the transition graph.
- Transition graph lacks an edge used by a rule.

Repair:

1. Identify source state and target state.
2. Decide whether the transition is intended.
3. If intended, add the transition edge.
4. If not intended, fix the rule outcome or precondition.
5. Rerun `check`, then `analyse`.

### `missing-state-field`

Symptoms:

- Rule enters a state with state-dependent required fields but does not set them.

Repair:

1. Identify fields required by the target state.
2. Add missing `ensures` assignments in the transitioning rule.
3. If the data should not be available at that point, revise the lifecycle instead.
4. Rerun `check`, then `analyse`.

### `unreachable-rule`

Symptoms:

- Analysis indicates a rule cannot fire or a condition cannot be reached.

Repair:

1. Trace the rule's `requires` conditions.
2. Identify missing prior rule, surface, field, or transition.
3. Add the missing path or tighten/remove the unreachable rule if it was erroneous.
4. Rerun `check`, then `analyse`.

### `conflicting-rule`

Symptoms:

- Analysis indicates overlapping behaviors or incompatible outcomes.

Repair:

1. Identify rules that can fire under the same conditions.
2. Decide intended precedence or separation.
3. Add explicit preconditions to separate cases.
4. If both behaviors are intended, model the combined outcome explicitly.
5. Rerun `analyse`.

### `dead-end-state`

Symptoms:

- Analysis indicates an entity can reach a non-terminal state with no valid exit.

Repair:

1. Decide whether the state is terminal.
2. Add it to terminal states or add valid outgoing transitions.
3. Ensure rules exist for the outgoing transitions.
4. Rerun `analyse`.

### `invariant-violation`

Symptoms:

- A rule or reachable state may violate an invariant.

Repair:

1. Treat the invariant as authoritative unless the user changed domain intent.
2. Add missing preconditions or outcome assignments.
3. Avoid weakening the invariant to make analysis pass.
4. Rerun `check`, then `analyse`.

### `empty-model`

Symptoms:

- `allium model` returns only metadata or an unexpectedly tiny object.

Repair:

```bash
find "$SPEC_DIR" -type f -name '*.allium' | sort
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
allium parse "$ENTRYPOINT"
```

Then inspect disconnected imports or a wrong entrypoint.

### `unexpected-plan-output`

Symptoms:

- `allium plan` produces no obligations, irrelevant obligations, or fails after `check`/`analyse` passed.

Repair:

1. Run `allium model "$ENTRYPOINT"`.
2. Verify rules and invariants are visible.
3. Inspect whether the spec has actual testable behavior.
4. If needed, improve rule specificity and rerun `check`, `analyse`, `plan`.

## Escalation rules

Use `parse` only when syntax structure is unclear.

Use `model` when the issue may be entrypoint visibility or disconnected imports.

Use `analyse` after `check` passes and the issue is semantic or process-level.

Use `plan` only after `check` and `analyse` pass.

## Ambiguity policy

If the repair requires changing domain intent, do not guess. Report the ambiguity with options.

Examples requiring clarification:

- Whether a state should be terminal.
- Whether a forbidden transition should become allowed.
- Whether an invariant is too strict or a rule is incomplete.
- Whether conflicting rules represent priority, error, or separate cases.

When a best-effort repair is safe and local, perform it. When the domain behavior would change materially, stop and ask.

## Final verification after repair

After repairing a diagnostic, run the smallest required verification sequence:

### Syntax/validation repair

```bash
allium check "$ENTRYPOINT"
```

### Semantic repair

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

### Model visibility repair

```bash
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
```

### Test planning repair

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```


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

- `allium-cli-output-interpretation` for reporting diagnostics.
- `allium-cli-parse` for syntax structure.
- `allium-cli-model` for visibility issues.
- `allium-cli-analyse` for semantic findings.
