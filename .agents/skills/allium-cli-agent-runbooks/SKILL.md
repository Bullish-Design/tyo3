---
name: allium-cli-agent-runbooks
description: Use for practical command recipes when agents operate Allium CLI commands on a folder of .allium specs.
---

# Allium CLI Agent Runbooks

Use this skill when an agent needs a direct recipe for common Allium CLI tasks at folder level.

## Primary objective

Provide reliable command sequences that resolve a folder of `.allium` files to an entrypoint and run the correct CLI commands in the correct order.


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


## Runbook: validate a spec folder

Use when the user asks whether a folder of specs is valid.

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
```

If it fails, use `allium-cli-diagnostics-repair`.

## Runbook: validate after editing a spec

Use immediately after creating or editing any `.allium` file.

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
```

Do not validate only the edited file if an entrypoint exists.

## Runbook: review before implementation

Use when specs will drive code changes.

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

Proceed to implementation only after both pass or after clearly reporting unresolved diagnostics.

## Runbook: generate test obligations

Use before writing tests from Allium specs.

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

If saving output:

```bash
mkdir -p artifacts
allium plan "$ENTRYPOINT" > artifacts/allium-test-plan.txt
```

## Runbook: extract domain model

Use when the user asks what the spec folder defines, or when downstream tooling needs structured model output.

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
```

If saving output:

```bash
mkdir -p artifacts
allium model "$ENTRYPOINT" > artifacts/allium-model.json
```

## Runbook: debug confusing syntax

Use when `check` fails but the diagnostic is hard to interpret.

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
TARGET_FILE="specs/allium/problem-file.allium"
allium parse "$TARGET_FILE"
allium check "$ENTRYPOINT"
```

Patch the smallest syntax region, then rerun both commands as needed.

## Runbook: diagnose unexpectedly empty model output

Use when `allium model` returns only version metadata or omits expected declarations.

```bash
SPEC_DIR="specs/allium"
find "$SPEC_DIR" -type f -name '*.allium' | sort
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
allium parse "$ENTRYPOINT"
```

If the entrypoint parses but model output is still sparse, inspect imports or aggregation conventions.

## Runbook: compare before/after model changes

Use when reviewing a spec edit.

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
mkdir -p artifacts
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT" > artifacts/allium-model.before.json
# apply or inspect changes
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT" > artifacts/allium-model.after.json
```

Then compare expected and observed model deltas.

## Runbook: full pre-merge CLI gate

Use before finalizing a spec-related change.

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

Optional artifacts:

```bash
mkdir -p artifacts
allium model "$ENTRYPOINT" > artifacts/allium-model.json
allium plan "$ENTRYPOINT" > artifacts/allium-test-plan.txt
```

## Runbook: unknown folder layout

Use when the user provides a folder that does not follow known conventions.

```bash
SPEC_DIR="path/from/user"
find "$SPEC_DIR" -type f -name '*.allium' | sort
```

Then:

1. Use explicit user entrypoint if provided.
2. Use `index.allium` if present.
3. Use the only `.allium` file if exactly one exists.
4. Otherwise inspect likely aggregate files.
5. If still ambiguous, report candidate entrypoints.

## Runbook: command failure

Use for any failed command.

```text
1. Stop the current sequence.
2. Copy exact command and diagnostic.
3. Classify the error.
4. Patch minimally.
5. Rerun the same command.
6. Continue only after it passes.
```

## Required final summary

At the end of a runbook execution, report:

- Folder used.
- Entrypoint resolved.
- Commands run.
- Pass/fail status.
- Any repairs made.
- Remaining blockers.


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

- `allium-cli-overview` for choosing a flow.
- `allium-cli-diagnostics-repair` for failures.
- `allium-cli-output-interpretation` for final reporting.
