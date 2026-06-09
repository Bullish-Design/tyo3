# TyO3 — durable identity model (developer)

Durable identity is the spine the whole system hangs off: a stable id per
entity (function / method / class) that lets authored and derived data stay
attached across edits. This page records exactly what preserves an id and what
does not — verified by probing the daemon, not assumed.

## What preserves an entity's durable id

| Operation                                   | Binds as  | id kept? | notes follow? |
|---------------------------------------------|-----------|----------|---------------|
| In-place body edit (same def name)          | `changed` | ✅        | ✅            |
| Atomic move (`sync_buffers` / `edit_many`)  | `moved`   | ✅        | ✅            |
| Rename the def/class **name**               | add+remove| ❌        | ❌            |

- **Body edit.** Keep the signature/name, change the body: the entity stays in
  the same slot, binds as `changed`, the id is stable, and the authored note
  (keyed by id) stays attached.
- **Atomic move.** Relocating an entity to another file *in a single commit*,
  appending the body verbatim so the content hash is unchanged, binds as
  `moved`: same id, new location, notes + summary ride along. This is what
  `:TyO3Move` does — never two separate edits, which would not guarantee it.
- **Rename.** Changing the *definition name* via a normal `sync_buffer` is
  remove-old + add-new: a **new** id, and the note does **not** carry over.
  Treat a rename as creating a new entity.

> The plugin README's shorthand "glued through a rename, a move, and a reformat"
> is aspirational for the rename case — only edits and atomic moves are
> guaranteed today. Demos and docs should show the move, not a bare rename.

## Reads run over a frozen snapshot

`entity_at` and friends resolve against the last committed snapshot, so the card
reflects saved/synced state. The cursor-driven CONTEXT reads therefore lag the
buffer by one debounced commit — intentional, and why `last_affected_revision`
reflects the last *committed* change.

## `entity_at` resolution quirk

`entity_at(line, col)` on a **nested method's** `def` line resolves to the
enclosing **class**, not the method. Land affected-state lookups on top-level
functions for clean per-entity resolution. `last_affected_revision` tracks when
the entity *itself* last changed — a caller pulled transitively into an affected
set does not see its own `affected@` advance.

## The layers attached to the spine

Each entity carries layers, separated in the sidebar by origin:

- **authored** (e.g. `intent` notes) — human-entered, survive by id.
- **derived** (e.g. `summary`, `embed`) — generated from code, recomputed on
  change, served stale-until-fresh.
