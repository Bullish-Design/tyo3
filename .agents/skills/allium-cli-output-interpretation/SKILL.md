---
name: allium-cli-output-interpretation
description: Use when summarizing, comparing, or acting on Allium CLI output from commands run against a folder of .allium specs.
---

# Allium CLI Output Interpretation

Use this skill to turn `allium` CLI output into accurate, actionable summaries for humans or downstream agents.

## Primary objective

Preserve exact command context, distinguish success from failure, classify findings, and recommend the next smallest useful action.


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


## Standard output summary format

Use this format for command results:

```text
Spec folder:
  <SPEC_DIR>

Entrypoint:
  <ENTRYPOINT>

Command:
  <exact command>

Result:
  Passed | Failed | Inconclusive

Blocking diagnostics:
  - <diagnostic summary>

Likely cause:
  - <cause if inferable>

Action taken or recommended:
  - <minimal next action>
```

If multiple commands ran, summarize each in sequence and show where the sequence stopped.

## Do not hide failures

If a command fails, the final answer must say it failed. Do not report the task as successful because a later unrelated command ran or because partial output was produced.

Bad:

```text
Validation mostly worked and I generated the model.
```

Good:

```text
`allium check specs/allium/index.allium` failed, so I did not treat the model output as reliable. The blocking diagnostic is ...
```

## Classifying result status

### Passed

Use when the command exits successfully and reports no blocking diagnostics.

### Failed

Use when the command exits non-zero or reports blocking diagnostics.

### Inconclusive

Use when:

- Output was truncated.
- Entrypoint was ambiguous.
- The command was run against a fallback file.
- Output appears inconsistent with folder contents.
- The command succeeded but the result is unexpectedly sparse.

Inconclusive output requires a next action such as resolving entrypoint, rerunning with full output, or using `model`/`parse`.

## Interpreting command-specific output

### `check`

Focus on:

- File/span.
- Syntax vs structural issue.
- Whether validation stopped early.
- Whether the entrypoint was correct.

Next action after failure: repair and rerun the same `check` command.

### `analyse`

Focus on:

- Process-level finding category.
- Affected entity/rule/state/invariant.
- Whether the finding blocks implementation or test generation.
- Whether domain intent is ambiguous.

Next action after failure: repair semantic/process issue, then run `check` and `analyse`.

### `parse`

Focus on:

- Parsed structure relevant to the suspected issue.
- Difference between intended structure and actual syntax tree.
- The smallest syntax region to patch.

Next action: rerun `parse` if needed, then `check` against entrypoint.

### `model`

Focus on:

- Visible entities/rules/transitions/invariants.
- Whether expected declarations are present.
- Whether output is unexpectedly sparse.
- Whether imports appear disconnected.

Next action after suspicious output: verify entrypoint, run `check`, inspect imports, possibly run `parse`.

### `plan`

Focus on:

- Obligation groups.
- Rule/lifecycle/invariant coverage.
- Missing negative cases.
- Ambiguous or duplicate obligations.

Next action: map obligations to repository tests or update the spec if obligations expose ambiguity.

## Comparing outputs across runs

When comparing before/after CLI output, report only meaningful deltas:

```text
Expected delta:
  - Added Order.BackorderPlaced behavior.

Observed CLI delta:
  - Model now includes BackorderPlaced rule.
  - Plan now includes obligations for deferred payment capture.

Unexpected delta:
  - CancelOrder disappeared from the model.
```

Unexpected deltas should trigger investigation before proceeding.

## Handling large output

If output is large:

1. Preserve raw output in a file only if useful or requested.
2. Summarize high-level findings.
3. Quote only the relevant diagnostic lines.
4. Avoid dumping full parse trees or large model JSON into the final answer.

Suggested artifact names:

```bash
artifacts/allium-check.txt
artifacts/allium-analyse.txt
artifacts/allium-model.json
artifacts/allium-test-plan.txt
```

## Human-facing final response patterns

### Successful validation

```text
Resolved `specs/allium` to `specs/allium/index.allium`. `allium check` passed. No blocking validation diagnostics were reported.
```

### Failed validation

```text
Resolved `specs/allium` to `specs/allium/index.allium`. `allium check` failed. The blocking diagnostic is a missing reference to `PaymentAuthorized` in `orders.allium`. The next repair is to correct the reference or connect the file declaring it through the entrypoint.
```

### Successful semantic review

```text
`allium check` and `allium analyse` both passed for `specs/allium/index.allium`. The spec folder is ready for implementation planning from the CLI's perspective.
```

### Inconclusive model extraction

```text
`allium model` succeeded, but the output only contained version metadata. That is likely a wrong-entrypoint or disconnected-import problem. I would next inspect the entrypoint and run `allium parse` on it.
```

## Reporting anti-patterns

Do not:

- Omit the exact command.
- Say “validated” when only `parse` ran.
- Say “semantically clean” when only `check` ran.
- Treat empty model output as normal if declarations were expected.
- Generate tests from failed or inconclusive `plan` output.


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

- `allium-cli-diagnostics-repair` for failed commands.
- `allium-cli-model` for structured output.
- `allium-cli-plan` for test obligations.
