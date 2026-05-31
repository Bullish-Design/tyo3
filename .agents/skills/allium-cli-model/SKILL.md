---
name: allium-cli-model
description: Use when extracting or inspecting the structured domain model from a folder of .allium specs using allium model.
---

# Allium CLI Model

Use this skill when the user wants to inspect the domain model represented by a folder of `.allium` specs, compare model output before and after edits, or diagnose unexpectedly sparse model output.

## Primary objective

Run `allium model` against the resolved entrypoint file and interpret the structured model output as the CLI sees it.


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

If `check` fails, repair validation before relying on `model` output.

## Standard workflow

```bash
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
```

For saved output:

```bash
mkdir -p artifacts
allium model "$ENTRYPOINT" > artifacts/allium-model.json
```

Only save output when requested, when useful for comparison, or when the repo expects artifacts.

## What `model` is for

Use `model` to confirm:

- Which entities are visible from the entrypoint.
- Which fields, statuses, relationships, projections, or derived values are visible.
- Which rules are visible.
- Which transitions and terminal states are visible.
- Which invariants are visible.
- Whether imports are connected to the entrypoint.
- Whether a recent edit changed the model as expected.

## Interpreting sparse output

If output is unexpectedly sparse, especially if it only contains a version field, treat it as a diagnostic signal.

Check these in order:

```text
1. Was a file passed, not a directory?
2. Was the file the actual root entrypoint?
3. Does the entrypoint import or contain the expected specs?
4. Do imported files actually contain valid declarations?
5. Does allium check pass on the same entrypoint?
6. Did the expected declarations parse as intended?
```

Recommended command sequence:

```bash
find "$SPEC_DIR" -type f -name '*.allium' | sort
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
allium parse "$ENTRYPOINT"
```

If a specific leaf file is expected to contribute declarations, parse that leaf too:

```bash
allium parse "$TARGET_FILE"
```

Then rerun:

```bash
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
```

## Before/after comparison workflow

Use this when reviewing a spec edit.

```bash
mkdir -p artifacts
allium model "$ENTRYPOINT" > artifacts/allium-model.before.json
# make or inspect spec changes
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT" > artifacts/allium-model.after.json
```

Compare the model outputs for the intended delta. Do not assume every textual spec change should produce model output changes; comments or reformatting may not affect the model.

## What to look for in model review

For each expected change, verify the model contains the corresponding structure:

- New entity appears.
- New field appears under the correct entity.
- Status enum includes expected states.
- Transition graph includes expected edges.
- Terminal states are present.
- Rule names are present and correctly scoped.
- Preconditions and outcomes are visible in the extracted representation, if supported.
- Invariants are present.

## Common model issues

### Wrong entrypoint

Symptom: valid output exists but does not include expected files.

Repair: choose the entrypoint that imports or aggregates the spec folder.

### Disconnected leaf file

Symptom: a leaf file contains declarations, but `model` output omits them.

Repair: connect the leaf file through the entrypoint/import graph according to project conventions.

### Parsed but not modeled

Symptom: `parse` sees structure but `model` output omits it.

Repair: run `check`; inspect whether the structure is legal domain-model content or merely syntactically parseable.

### Model change too broad

Symptom: after a small edit, large unrelated model changes appear.

Repair: inspect accidental import changes, brace movement, renamed declarations, or entrypoint change.

## Folder-level examples

### Extract model for canonical specs

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
```

### Save model output

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
mkdir -p artifacts
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT" > artifacts/allium-model.json
```

## Reporting requirements for model output

Summarize model output in terms of domain structure. Avoid dumping large JSON unless the user requested raw output.

Report:

- Resolved folder and entrypoint.
- Whether `check` passed first.
- Number or names of visible entities/rules when practical.
- Whether expected declarations are present.
- Any signs of wrong entrypoint or disconnected imports.


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

- `allium-cli-check` before extraction.
- `allium-cli-parse` when the model is sparse or unexpected.
- `allium-cli-output-interpretation` for summarizing JSON output.
