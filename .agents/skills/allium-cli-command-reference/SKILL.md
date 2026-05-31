---
name: allium-cli-command-reference
description: Use as a concise reference for Allium CLI commands when working on a folder of .allium specs. Assumes the CLI is already available and entrypoint resolution is required.
---

# Allium CLI Command Reference

This skill is a compact command reference for agents using the `allium` CLI against a folder of `.allium` specifications. It does not install the CLI and does not replace command-specific skills.


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


## Commands

### `allium check <entrypoint.allium>`

Validates specifications and reports diagnostics.

Use it:

- After every `.allium` edit.
- Before `analyse`, `model`, or `plan`.
- As the first command in almost every folder-level CLI task.

Example:

```bash
allium check "$ENTRYPOINT"
```

Escalate to `allium-cli-diagnostics-repair` if it fails.

### `allium analyse <entrypoint.allium>`

Runs deeper structural and process-level analysis.

Use it:

- Before implementation from specs.
- Before generating tests.
- To inspect reachability, lifecycle, data-flow, conflict, deadlock, and invariant issues.

Example:

```bash
allium analyse "$ENTRYPOINT"
```

Run `check` first unless you are specifically investigating an analysis command failure.

### `allium parse <file.allium>`

Parses a file and emits the syntax tree.

Use it:

- To debug confusing syntax errors.
- To verify that a block is parsed as intended.
- To inspect a leaf file with localized syntax problems.

Example:

```bash
allium parse "$TARGET_FILE"
```

`parse` is not a semantic validator. Follow it with `check "$ENTRYPOINT"`.

### `allium model <entrypoint.allium>`

Extracts the domain model as structured data.

Use it:

- To confirm what the CLI sees from the entrypoint.
- To detect disconnected imports.
- To generate or review documentation inputs.
- To compare before/after spec edits.

Example:

```bash
allium model "$ENTRYPOINT"
```

If the model is unexpectedly sparse, verify that `ENTRYPOINT` is correct and that imports are connected.

### `allium plan <entrypoint.allium>`

Derives test obligations from a specification.

Use it:

- Before generating tests from Allium specs.
- To identify behavioral coverage obligations.
- To map rules, transitions, and invariants to test cases.

Example:

```bash
allium plan "$ENTRYPOINT"
```

Run `check` and `analyse` first.

## Safe command sequences

### Validation sequence

```bash
allium check "$ENTRYPOINT"
```

### Semantic review sequence

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

### Model extraction sequence

```bash
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
```

### Test planning sequence

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

### Syntax debugging sequence

```bash
allium parse "$TARGET_FILE"
allium check "$ENTRYPOINT"
```

## Command selection table

| User request | Command sequence |
|---|---|
| “Validate this folder” | `check` |
| “Is this spec semantically clean?” | `check`, `analyse` |
| “Why is this syntax failing?” | `parse` target file, then `check` entrypoint |
| “What does the spec model contain?” | `check`, `model` |
| “Generate tests from this spec” | `check`, `analyse`, `plan` |
| “Review before implementation” | `check`, `analyse` |

## Common mistakes

- Passing the folder path instead of the entrypoint file.
- Running `parse` and assuming the spec is valid.
- Running `plan` before `analyse` passes.
- Running commands on a leaf file when the entrypoint is required.
- Ignoring unexpectedly sparse `model` output.


## Reporting requirements

When reporting command results, include:

- The exact command run.
- Whether it passed or failed.
- The resolved spec folder.
- The resolved entrypoint.
- Blocking diagnostics, if any.
- The smallest meaningful next action.

Do not claim a spec is valid unless the relevant command completed successfully against the resolved entrypoint.

