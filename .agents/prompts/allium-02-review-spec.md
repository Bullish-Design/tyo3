# Allium — Review Specification Alignment

Review the Allium specs in `{{ spec_output_dir }}/` for alignment with Allium principles.

## Preparation

Before reviewing, re-read the Allium concept by loading the **allium** skill and its language reference (`references/language-reference.md`). Make sure you understand:

- The role of entities, surfaces, rules, and invariants
- The difference between behavioural contracts and implementation detail
- What `@guidance` is for

## Review checklist

For each spec file, verify:

### Correct abstraction level
- [ ] Entities model behaviour, not data schemas
- [ ] No implementation details leaked into entities (file paths, CLI flags, env vars)
- [ ] Rules express `when ... ensures ...` without prescribing *how*

### No overspecification
- [ ] Entities carry only fields that rules actually reference
- [ ] Surfaces enumerate events exhaustively — no missing trigger paths
- [ ] Invariants express non-negotiable constraints, not nice-to-haves

### Structural correctness
- [ ] Every rule has a `when:` that references a surface event
- [ ] Every `ensures:` clause is testable (observable state change or event emission)
- [ ] Conditional fields use `when` correctly in entity definitions
- [ ] Cross-file references use `use ".../file.allium" as alias` syntax

### Spec-to-spec consistency
- [ ] Entity names are consistent across files
- [ ] Events emitted in one spec are consumed in another where expected
- [ ] No orphaned events or unreachable rules

## Output

For each spec file, report:
- **Pass** — no issues found
- **Minor** — style or clarity improvement suggested (list them)
- **Blocking** — rule/invariant/surface error that must be fixed (list them)

Do not modify the specs. Only report findings.

## Reference skills

- `allium` — language reference and patterns
- `tend` — for applying fixes after review
