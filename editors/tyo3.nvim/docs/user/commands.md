# TyO3 — command reference

Every user-facing command. Most operate on the entity under the cursor in a
Python buffer inside a TyO3 project (a directory with `.tyo3/`, `pyproject.toml`,
or `.git`).

## Navigating & inspecting

- `:TyO3Inspect` — open the full cross-layer card for the entity under the
  cursor (durable id, kind, location, content hash, authored + derived layers,
  last-affecting revision) in a dismissable float.
- `:TyO3Context` — toggle cursor-driven CONTEXT tracking (`"cursor"` ⇄ `"off"`).
  When on, the sidebar's identity/notes/summary panes follow your cursor.
- `:TyO3Panel` — toggle the side dock.
- `:TyO3Docs` — open the documentation index (these files) in a split.

## Authoring

- `:TyO3Note <text>` — author an `intent` note on the entity under the cursor.
  With no text you are prompted. The note renders inline (🏷) and survives edits
  and moves.

## Structural edits

- `:TyO3Move <name> <dest_path>` — atomically move the named entity to
  `<dest_path>` (project-relative). One commit ⇒ the durable id, notes, and
  summary are preserved (a *Moved* bind).

## History & impact

- `:TyO3Diff [from_rev] [to_rev]` — entity-level diff (added / removed / changed
  / moved) between two revisions. No args ⇒ previous revision vs. head.
- `:TyO3Affected` — list the last edit's affected set; pick one to jump to it by
  identity.
- `:TyO3Entities` / `:TyO3Authored` — pick across all known entities / all
  authored notes and jump.

## Daemon lifecycle & ops

- `:TyO3Start` / `:TyO3Stop` — attach / stop the daemon for this project.
- `:TyO3Reindex` — full rescan (`sync_all`).
- `:TyO3Gc` — evict orphaned derived artifacts.
- `:TyO3Check` — run the type-checker over the project.
- `:TyO3DaemonLog` — show the captured daemon stderr log.

## Sidebar keys (inside the dock)

- `<Tab>` — collapse / expand the pane under the cursor.
- `<CR>` — activate the current line: toggle a header, run an action, or open a
  linked doc.
- `gd` — open the doc linked to the current pane (when one exists).
