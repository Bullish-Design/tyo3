---
name: allium-cli-parse
description: Use when debugging syntax or parsed structure for one or more .allium files inside a folder, then returning to entrypoint-level validation.
---

# Allium CLI Parse

Use this skill when a folder-level CLI task reveals confusing syntax, malformed structure, or unexpected parser behavior in a `.allium` file.

## Primary objective

Use `allium parse` to inspect the syntax tree of a specific `.allium` file, then return to `allium check` against the resolved folder entrypoint.


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


## Important distinction

`allium parse` accepts a spec file and outputs syntax-tree structure. It is useful for debugging structure, but it is not proof that the spec set is valid or semantically complete.

Always follow syntax debugging with:

```bash
allium check "$ENTRYPOINT"
```

## When to use `parse`

Use `parse` when:

- `allium check` diagnostics point to syntax but the cause is unclear.
- A rule, entity, transition, invariant, or config block appears to parse differently than intended.
- A block may be nested incorrectly.
- Braces or indentation make the visible structure misleading.
- You need to inspect a leaf file without validating the entire folder on every step.

Do not use `parse` as the first command for ordinary validation. Start with `check`.

## Selecting `TARGET_FILE`

Choose `TARGET_FILE` using this order:

1. The file named in the diagnostic.
2. The file the user asked about.
3. The most recently edited `.allium` file.
4. The entrypoint file if the issue appears in imports or top-level structure.

If no target file is clear, run `check` first and use its diagnostic to select the file.

## Standard syntax-debugging workflow

```bash
allium check "$ENTRYPOINT"
allium parse "$TARGET_FILE"
# patch the smallest malformed construct
allium parse "$TARGET_FILE"
allium check "$ENTRYPOINT"
```

Do not keep parsing repeatedly after the structure is clear. Return to validation.

## What to inspect in parse output

Look for:

- Whether top-level declarations are recognized as top-level declarations.
- Whether a `rule` body contains the intended `when`, `requires`, `let`, and `ensures` clauses.
- Whether an `entity` body includes fields, transitions, and invariants at the intended nesting level.
- Whether comments or text are being interpreted unexpectedly.
- Whether braces close where the author intended.
- Whether an import or version marker is recognized.

## Common parse-driven repairs

### Mis-nested rule

Symptom: a rule appears inside an entity or another block unexpectedly.

Repair:

- Move the rule to the intended top-level location.
- Fix the brace that caused nesting.
- Rerun `parse`, then `check`.

### Misplaced `ensures`

Symptom: intended outcome lines are not parsed under `ensures`.

Repair:

- Correct block formatting.
- Ensure outcomes belong to the intended rule.
- Rerun `parse`, then `check`.

### Malformed transition graph

Symptom: transition declarations do not appear under the intended `transitions` block.

Repair:

- Fix transition block braces.
- Normalize transition line structure.
- Rerun `parse`, then `check`.

### Invalid expression shape

Symptom: parse tree stops or changes shape near an invariant, precondition, or derived expression.

Repair:

- Simplify the expression temporarily.
- Reintroduce complexity after `check` passes.
- Avoid broad rewrites.

## Folder-level examples

### Debug one leaf file from a folder

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
TARGET_FILE="specs/allium/orders.allium"
allium parse "$TARGET_FILE"
allium check "$ENTRYPOINT"
```

### Debug the entrypoint itself

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium parse "$ENTRYPOINT"
allium check "$ENTRYPOINT"
```

## Reporting parse findings

When reporting parse output, do not dump the entire tree unless it is small and directly relevant. Summarize:

- What file was parsed.
- What structure was expected.
- What structure the parser appears to see.
- The smallest syntax correction made or recommended.
- The result of the follow-up `check`.


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

- `allium-cli-check` for validation.
- `allium-cli-diagnostics-repair` for failure loops.
- `allium-cli-output-interpretation` for summarizing large parser output.
