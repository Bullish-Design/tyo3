---
name: allium-cli-plan
description: Use when deriving test obligations from a folder of .allium specs with allium plan after check and analyse pass.
---

# Allium CLI Plan

Use this skill when the user wants test obligations, test planning, or test generation guidance from a folder of `.allium` specs.

## Primary objective

Run `allium plan` against the resolved entrypoint only after `check` and `analyse` pass, then interpret the output as test obligations rather than final test code.


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

Run these first:

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
```

Do not use `plan` output as a basis for test generation if `check` or `analyse` fails.

## Standard workflow

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

For saved output:

```bash
mkdir -p artifacts
allium plan "$ENTRYPOINT" > artifacts/allium-test-plan.txt
```

Only save output when requested or useful for downstream work.

## What `plan` is for

Use `plan` to derive behavioral test obligations from the specification. Treat the output as a planning source that must be mapped into the repository's test architecture.

Potential obligation categories:

- Rule trigger tests.
- Preconditions and rejection tests.
- Outcome tests.
- Lifecycle transition tests.
- Terminal-state tests.
- Invariant preservation tests.
- Cross-entity consistency tests.
- Negative tests for forbidden transitions or missing preconditions.

## Mapping obligations to tests

Use this mapping unless the repository has stronger conventions:

| Obligation type | Likely test type |
|---|---|
| Single rule precondition/outcome | Unit or integration test |
| Multi-entity rule behavior | Integration test |
| Full lifecycle path | State-machine or integration test |
| Invariant preservation | Property or integration test |
| User-visible behavior | End-to-end or API-level test |
| Error/denial behavior | Negative unit/integration test |

Keep tests traceable to Allium rule names. Prefer test names that include entity/rule identifiers.

Example naming pattern:

```text
Order_ShipOrder_requires_confirmed_status
Order_ShipOrder_sets_tracking_number_and_shipped_at
Order_CancelOrder_rejects_delivered_orders
```

## Review obligations before generating tests

Before writing tests from `plan` output, inspect for:

- Missing negative cases.
- Missing terminal-state coverage.
- Duplicate obligations.
- Obligations that assume implementation details not present in the spec.
- Overly broad obligations that need decomposition.
- Ambiguous behavior that should be clarified in the spec first.

If the plan reveals ambiguity, update the spec and rerun:

```bash
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

## Folder-level examples

### Generate plan for canonical specs

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT"
```

### Save a test plan artifact

```bash
SPEC_DIR="specs/allium"
ENTRYPOINT="specs/allium/index.allium"
mkdir -p artifacts
allium check "$ENTRYPOINT"
allium analyse "$ENTRYPOINT"
allium plan "$ENTRYPOINT" > artifacts/allium-test-plan.txt
```

## Failure handling

If `check` fails, use `allium-cli-check` and `allium-cli-diagnostics-repair`.

If `analyse` fails, use `allium-cli-analyse` and repair semantic/process diagnostics.

If `plan` fails after both passed:

1. Preserve the exact command and output.
2. Check whether the failure points to unsupported spec structure, missing declarations, or an internal CLI limitation.
3. Run `model` to see whether the expected structures are extractable.
4. Repair spec issues if clear.
5. Otherwise report the limitation with the exact reproduction command.

## Success criteria

A successful `plan` run means:

- The CLI produced test obligations from the entrypoint.
- The spec passed validation and analysis first.
- The output can be used to design tests.

It does not mean:

- Tests have been written.
- The generated obligations are exhaustive relative to unstated business intent.
- The repository's existing tests already satisfy the obligations.

## Reporting requirements for plan output

Report:

- Resolved folder and entrypoint.
- Whether `check` and `analyse` passed first.
- Main obligation groups.
- Any missing or ambiguous coverage noticed.
- Recommended next test implementation step.


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

- `allium-cli-check` and `allium-cli-analyse` before planning.
- `allium-cli-output-interpretation` for summarizing large plan output.
- `/allium:propagate` or equivalent workflow skills for actually generating tests.
