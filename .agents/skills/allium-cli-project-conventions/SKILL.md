---
name: allium-cli-project-conventions
description: Use when operating the Allium CLI on a project folder of .allium specs and applying repo-specific conventions for entrypoints, command wrappers, quality gates, and artifact locations. Assumes the allium CLI is already installed and working.
---

# Allium CLI Project Conventions

This skill defines project-level conventions for using the `allium` CLI on a folder of `.allium` specifications. It is not an installation guide. It assumes the CLI is available and focuses on consistent invocation, entrypoint resolution, validation, and reporting.

## Primary objective

Given a folder of `.allium` files, identify the effective spec entrypoint and run CLI commands against that file rather than the folder itself. Use these conventions before applying any command-specific Allium CLI skill.


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


## Canonical project assumptions

Prefer these defaults unless the repository says otherwise:

```text
Canonical spec folder:      specs/allium/
Canonical entrypoint:       specs/allium/index.allium
Scratch spec folder:        .scratch/specs/allium/
Scratch entrypoint:         .scratch/specs/allium/index.allium
Required validation:        allium check <entrypoint>
Required semantic review:   allium analyse <entrypoint>
Optional model artifact:    artifacts/allium-model.json
Optional plan artifact:     artifacts/allium-test-plan.txt
```

If the user's folder is not one of these paths, resolve the entrypoint from the actual folder contents rather than forcing this layout.

## Command wrapper convention

If the repository provides a wrapper script, use it. Otherwise call `allium` directly. Do not introduce install or environment setup work in this skill.

Common wrapper patterns:

```bash
./scripts/allium-check "$ENTRYPOINT"
./scripts/allium-analyse "$ENTRYPOINT"
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

When both a wrapper and bare `allium` are available, prefer the wrapper because it captures repository policy.

## Mandatory command policy

After any `.allium` edit, run:

```bash
allium check "$ENTRYPOINT"
```

Before implementation from a materially changed spec, run:

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

Before test generation from a spec, run:

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

Before claiming model extraction is correct, run:

```bash
allium check "$ENTRYPOINT"
allium model "$ENTRYPOINT"
```

## Folder-level behavior

The user may say:

```text
Use this skill on specs/allium
Validate .scratch/specs/allium
Run allium plan for the specs folder
```

Translate that folder request into a resolved entrypoint. For example:

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
```

Do not ask the user to provide the exact entrypoint if it can be resolved from normal conventions.

## Prohibited behavior

Do not:

- Pass a directory path to `allium check`, `allium analyse`, `allium model`, or `allium plan`.
- Validate only the changed leaf file when an entrypoint exists.
- Generate tests from `allium plan` if `allium analyse` has not passed.
- Treat `allium parse` output as proof that the spec is valid.
- Suppress CLI failures in the final response.
- Rewrite the whole spec set merely to fix a localized CLI diagnostic.

## Artifact policy

Only create command output artifacts when the user asked for them, when CI expects them, or when output is too large to use directly in conversation.

Recommended artifact names:

```bash
mkdir -p artifacts
allium model "$ENTRYPOINT" > artifacts/allium-model.json
allium plan "$ENTRYPOINT" > artifacts/allium-test-plan.txt
```

Do not commit generated artifacts unless the repository already treats them as tracked artifacts.


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

- Use `allium-cli-overview` to choose the right command sequence.
- Use `allium-cli-check` after editing specs.
- Use `allium-cli-analyse` before implementation or test generation.
- Use `allium-cli-diagnostics-repair` when a command fails.
