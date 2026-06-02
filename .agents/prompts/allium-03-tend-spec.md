# Allium — Refine Specification

Apply fixes and improvements to the Allium specs in `{{ spec_output_dir }}/`.

## Input

You should have review findings from the previous step (`allium-02-review-spec`). Address all **blocking** issues first, then **minor** improvements as appropriate.

## Process

Load the **tend** skill and follow its guidance for editing specs.

For each finding:

1. Identify the spec file and construct (entity, rule, surface, invariant)
2. Make the minimal edit to resolve the issue
3. Verify the edit doesn't break cross-file consistency (check `use` references)

## Key principles

- **Push back on vague requirements.** If a finding says "clarify X" but provides no concrete direction, flag it for the user rather than guessing
- **Preserve existing structure.** Refactor only when the structure is wrong; don't reorganize just because you'd do it differently
- **Keep guidance in `@guidance`.** If you're tempted to add detail to a rule, ask whether it's implementation advice (→ guidance) or behavioural contract (→ rule)

## After editing

Re-run `allium check` across all specs and confirm zero errors. Fix any regressions you introduced.

## Reference skills

- `tend` — writing, editing, and improving Allium specs
- `allium` — language reference, patterns, migrations
