---
name: allium-cli-check
description: Use when validating a folder of .allium specs with allium check. Resolves the folder to an entrypoint file, runs validation, interprets diagnostics, and defines the repair loop.
---

# Allium CLI Check

Use this skill whenever a user asks to validate a folder of `.allium` specs, after creating or editing a spec, or before moving to deeper analysis.

## Primary objective

Run `allium check` against the resolved entrypoint file for a folder of `.allium` specs and interpret the result accurately.


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


## Mandatory rule

After any `.allium` change, run:

```bash
allium check "$ENTRYPOINT"
```

Do not validate only the edited leaf file when an entrypoint exists. A leaf file may pass alone while the project-level model remains broken because imports, references, or cross-file relationships are wrong.

## What `check` is for

Use `allium check` to catch:

- Syntax errors.
- Invalid declarations.
- Missing or invalid references.
- Invalid language markers.
- Structural errors visible to local validation.
- Import or entrypoint mistakes that prevent the spec set from loading.

Do not use `check` as the only proof that the spec is semantically complete. Use `analyse` for deeper behavioral analysis.

## Standard workflow

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
```

If successful:

1. Record that `check` passed.
2. Continue to `analyse` if implementation, test generation, or semantic review is next.

If failed:

1. Do not proceed to `analyse`, `model`, or `plan` unless investigating the failure itself.
2. Preserve the exact diagnostic.
3. Use `allium-cli-diagnostics-repair`.
4. Patch minimally.
5. Rerun the same `check` command.

## Folder examples

### Validate canonical specs

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
```

### Validate scratch specs

```bash
SPEC_DIR=".scratch/specs/allium"
ENTRYPOINT=".scratch/specs/allium/index.allium"
allium check "$ENTRYPOINT"
```

### Validate a folder with one spec file

```bash
SPEC_DIR="specs/payment"
ENTRYPOINT="$(find "$SPEC_DIR" -type f -name '*.allium' | sort | head -n 1)"
allium check "$ENTRYPOINT"
```

Only use this single-file pattern when the folder contains exactly one `.allium` file.

## Diagnostic interpretation

Classify failed `check` output before editing.

### Syntax error

Likely causes:

- Missing brace.
- Misplaced block.
- Invalid field syntax.
- Invalid `requires` or `ensures` structure.

Next action:

```bash
allium parse "$TARGET_FILE"
```

Then patch the smallest syntactic region and rerun:

```bash
allium check "$ENTRYPOINT"
```

### Invalid version marker

Likely causes:

- Missing `-- allium: 3` marker.
- Wrong version marker for the current language.
- Marker not at the expected location.

Next action: patch the marker only, then rerun `check`.

### Missing reference

Likely causes:

- Entity, rule, field, enum value, or imported symbol is misspelled.
- Leaf file is disconnected from the entrypoint.
- Import graph is incomplete.

Next action:

1. Search for the declared symbol.
2. Verify it is imported or visible from `ENTRYPOINT`.
3. Patch the reference or import.
4. Rerun `check`.

### Directory passed instead of file

Likely symptom:

```text
Is a directory
```

Next action: resolve the entrypoint and rerun against the file.

```bash
allium check "$SPEC_DIR/index.allium"
```

## Minimal repair discipline

When `check` fails, do not perform a broad rewrite. Prefer this sequence:

```text
1. Identify the failing file/span.
2. Identify the smallest invalid construct.
3. Patch only that construct.
4. Rerun the exact same check command.
5. Continue only after it passes.
```

## Success criteria

A successful `check` run means:

- The CLI accepted the resolved entrypoint.
- The spec set loaded sufficiently for validation.
- No blocking validation diagnostics were reported.

It does not necessarily mean:

- Every lifecycle reaches a terminal state.
- Every rule composes into a complete process.
- Test obligations are complete.
- The spec matches implementation.

Use `analyse`, `plan`, or `/allium:weed` style workflows for those concerns.


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

- `allium-cli-diagnostics-repair` for failures.
- `allium-cli-parse` for confusing syntax.
- `allium-cli-analyse` after successful validation.
