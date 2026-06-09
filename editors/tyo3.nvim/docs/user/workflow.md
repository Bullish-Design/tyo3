# TyO3 — user workflow

TyO3 gives every function, method, and class in your project a **durable
identity** that survives edits, reformats, and moves. Notes and summaries you
attach stay glued to the *entity*, not to a line number. This guide is the
day-to-day workflow; for the exact commands see [commands](commands.md).

## The sidebar is your dashboard

Open a Python file in a TyO3 project and the daemon starts automatically. Toggle
the side dock with `:TyO3Panel`. It is split into collapsible panes, each a
different *kind* of data hanging off the entity under your cursor:

| Pane       | What it shows                                                |
|------------|--------------------------------------------------------------|
| `IDENTITY` | the entity's name · kind, durable id, and current location   |
| `NOTES`    | authored intent (your notes) attached to this entity         |
| `SUMMARY`  | derived artifacts (auto-generated summaries, embeddings)     |
| `ACTIONS`  | the tools you can run right now, in this context             |
| `DOCS`     | links to this documentation (user + developer)               |
| `AFFECTED` | the blast radius of your last edit, then its refinement      |

Move the cursor between definitions and the top panes track the entity under it.
Press `<Tab>` on a pane header to collapse/expand it; `<CR>` activates the line
(toggles a header, runs an action, or opens a doc).

## Turn on cursor tracking

The CONTEXT tracking is opt-in. Enable it per session with `:TyO3Context`, or set
`context = "cursor"` in `setup{}`. With it off, the dock only logs affected sets.

## Annotate intent that lasts

Park the cursor on a function and run `:TyO3Note this is the load-bearing path`.
The note appears inline (🏷) and in the `NOTES` pane. Rename the function's body,
reformat it, or move it to another file — the note follows, because it is keyed
to the entity's identity, not its text. (A *rename of the definition name* is the
one exception; see [durable identity](../dev/identity.md).)

## Watch the blast radius

Edit a function and save. The `AFFECTED` pane lights up with the id-level closure
of what your change touches, then an asynchronous **refinement** narrows it to
the precise set. This is how you see, immediately, what a change reaches.

## Move without losing context

`:TyO3Move checkout checkout.py` relocates an entity to another file in one
atomic commit. Its identity, its notes, and its summary all ride along. The
`IDENTITY` pane shows the same durable id with a new location.

## Navigate by identity

`:TyO3Inspect` opens the full cross-layer card for the entity under the cursor.
`:TyO3Diff` shows the entity-level (not textual) diff between revisions.
