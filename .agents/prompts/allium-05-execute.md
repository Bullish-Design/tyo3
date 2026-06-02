# Allium — Execute Implementation Phase

Execute a specific phase of the implementation guide for `{{ repo_name }}`.

## Input

The user will specify a phase number. If no phase is given, start at Phase 1.

The implementation guide is at `{{ impl_guide_path }}`. Read it before proceeding.

## Process

1. Read the implementation guide
2. Locate the requested phase
3. Check that all dependencies are satisfied (previous phases complete)
4. Implement the phase according to its description and acceptance criteria
5. Verify the acceptance criteria pass before moving on

## Rules

- **Do not skip phases.** If the user asks for Phase 4 but Phase 2 is incomplete, flag it
- **Stay within the phase.** Don't pre-implement future phases or refactor completed ones
- **Test as you go.** Each phase has acceptance criteria — verify them
- **Commit after each phase** with a message like `feat: phase N — <phase name>`

## After implementation

Run `allium check` on the specs to confirm they still pass. If the implementation introduces new behaviour not yet in the specs, flag it for the user — the specs may need updating via `allium-03-tend-spec`.

## Reference skills

- `weed` — check spec-code alignment after implementation
- `propagate` — generate tests from the specs to verify the phase
