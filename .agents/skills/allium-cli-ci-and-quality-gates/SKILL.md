---
name: allium-cli-ci-and-quality-gates
description: Use when defining or applying CI-quality command gates for a folder of .allium specs. Assumes allium is already available.
---

# Allium CLI CI and Quality Gates

Use this skill when the user wants automated quality gates, pre-merge checks, or repeatable validation commands for a folder of `.allium` specs.

## Primary objective

Define reliable folder-level CLI gates that resolve an entrypoint file and run the appropriate Allium commands without performing installation or environment setup.


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


## Baseline required gate

Every spec folder that has a stable entrypoint should pass:

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

This is the minimum pre-merge gate for spec changes.

## Optional generated-output gates

Use these when the project wants artifacts for review, documentation, or test planning:

```bash
mkdir -p artifacts
allium model "$ENTRYPOINT" > artifacts/allium-model.json
allium plan "$ENTRYPOINT" > artifacts/allium-test-plan.txt
```

These should not replace `check` and `analyse`.

## Gate levels

### Level 1: validation only

Use for fast feedback on every spec edit.

```bash
allium check "$ENTRYPOINT"
```

### Level 2: semantic gate

Use before PRs or implementation from specs.

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

### Level 3: implementation/test-readiness gate

Use before generating tests or implementation plans.

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

### Level 4: artifact gate

Use when CI should preserve domain model and test-obligation outputs.

```bash
mkdir -p artifacts
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium model "$ENTRYPOINT" > artifacts/allium-model.json
allium plan "$ENTRYPOINT" > artifacts/allium-test-plan.txt
```

## Multiple entrypoints

If a repository has multiple independent spec folders or bounded contexts, run gates per entrypoint.

Example:

```bash
for ENTRYPOINT in specs/allium/*/index.allium; do
  echo "Checking $ENTRYPOINT"
  allium check "$ENTRYPOINT"
  allium analyse "$ENTRYPOINT"
done
```

Do not assume one root entrypoint unless the repository convention says so.

## Failing fast

Quality gates should stop at the first failed prerequisite.

Correct order:

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

Incorrect order:

```bash
allium plan "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium check "$ENTRYPOINT"
```

## Script template

Use this as a repository script body when asked to create or review a gate. Adjust paths to repo conventions.

```bash
#!/usr/bin/env bash
set -euo pipefail

SPEC_DIR="${1:-specs/allium}"

if [[ -f "$SPEC_DIR/index.allium" ]]; then
  ENTRYPOINT="$SPEC_DIR/index.allium"
elif [[ -f "$SPEC_DIR/allium/index.allium" ]]; then
  ENTRYPOINT="$SPEC_DIR/allium/index.allium"
else
  mapfile -t SPECS < <(find "$SPEC_DIR" -type f -name '*.allium' | sort)
  if [[ "${#SPECS[@]}" -eq 1 ]]; then
    ENTRYPOINT="${SPECS[0]}"
  else
    echo "Could not resolve a unique Allium entrypoint under $SPEC_DIR" >&2
    printf '%s
' "${SPECS[@]}" >&2
    exit 2
  fi
fi

echo "Allium spec folder: $SPEC_DIR"
echo "Allium entrypoint: $ENTRYPOINT"

allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

## CI output policy

For CI logs:

- Echo the resolved entrypoint before running commands.
- Preserve raw CLI diagnostics.
- Do not hide failed command output behind a generic error.
- Store model/plan artifacts only if the project uses them.

## Common CI mistakes

- Passing `specs/allium/` instead of `specs/allium/index.allium`.
- Running per-file validation and missing import-level failures.
- Running `plan` even when `analyse` failed.
- Treating generated model or plan artifacts as canonical source.
- Letting CI use a different entrypoint than local agent workflows.

## Human review checklist

Before accepting a CI gate, verify:

- The gate resolves the same entrypoint agents use locally.
- `check` runs before `analyse`.
- `analyse` runs before `plan`.
- Failure output is visible.
- Multiple entrypoints are handled deliberately.
- Generated artifacts are either ignored or intentionally tracked.


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

- `allium-cli-project-conventions` for repo-specific paths.
- `allium-cli-agent-runbooks` for manual command equivalents.
- `allium-cli-output-interpretation` for CI result summaries.
