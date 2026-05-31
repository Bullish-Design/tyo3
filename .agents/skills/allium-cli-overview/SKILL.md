---
name: allium-cli-overview
description: Use as the operational entry point for running Allium CLI commands on a folder of .allium files. Chooses the right command sequence and resolves the folder to an entrypoint file.
---

# Allium CLI Overview

Use this skill when the task is to run, coordinate, or explain Allium CLI usage over a folder of `.allium` spec files. This skill routes to command-specific skills and defines the default command order.

## Primary objective

Convert a user request at folder level into the correct Allium CLI command sequence against a resolved spec entrypoint.


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


## Command map

```text
allium check    Validate specifications and report diagnostics.
allium analyse  Run deeper structural and process-level analysis.
allium parse    Parse a file and output the syntax tree.
allium model    Extract the domain model as structured data.
allium plan     Derive test obligations from a specification.
```

## Default command order

Use the lightest command that answers the user's request, then escalate only when needed.

### After editing or creating specs

```bash
allium check "$ENTRYPOINT"
```

### Before implementation work

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

### Before test generation

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

### To inspect the model visible to tooling

```bash
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
```

### To debug syntax structure

```bash
allium parse "$TARGET_FILE"
allium check "$ENTRYPOINT"
```

`TARGET_FILE` may be a leaf file if the syntax issue is localized. `ENTRYPOINT` should still be used for validation.

## Choosing the right command

Use `check` when:

- The user asks whether a folder of specs is valid.
- A `.allium` file was created or edited.
- You need a fast first-pass diagnostic.

Use `analyse` when:

- `check` passes but you need semantic confidence.
- The spec will guide implementation.
- The spec will guide test generation.
- You need lifecycle, reachability, conflict, data-flow, or invariant analysis.

Use `parse` when:

- `check` gives confusing syntax diagnostics.
- A block appears to parse differently than intended.
- You need to inspect syntax tree structure.

Use `model` when:

- You need to see what entities/rules/transitions/invariants the CLI sees.
- Model output will drive documentation, review, or downstream tooling.
- You need to detect disconnected imports or wrong entrypoints.

Use `plan` when:

- You need test obligations derived from the spec.
- The user asks for tests based on Allium.
- You need to review behavioral coverage before writing tests.

## Folder-level examples

### User asks: “Validate specs/allium”

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
```

### User asks: “Generate a test plan for .scratch/specs/allium”

```bash
SPEC_DIR=".scratch/specs/allium"
ENTRYPOINT=".scratch/specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

### User asks: “Why is this spec not parsing?”

```bash
allium parse "$TARGET_FILE"
allium check "$ENTRYPOINT"
```

## Failure handling

If any command fails:

1. Stop the current sequence.
2. Preserve the exact command and diagnostic.
3. Use `allium-cli-diagnostics-repair`.
4. Rerun the same command after the minimal repair.
5. Continue the sequence only after the failing command passes.

Do not run `plan` after failed `analyse`. Do not run `analyse` after failed `check` unless investigating a CLI behavior edge case.


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

- `allium-cli-command-reference` for concise command semantics.
- `allium-cli-check` for validation.
- `allium-cli-analyse` for semantic review.
- `allium-cli-model` for domain model extraction.
- `allium-cli-plan` for test obligations.
- `allium-cli-diagnostics-repair` for failed commands.
