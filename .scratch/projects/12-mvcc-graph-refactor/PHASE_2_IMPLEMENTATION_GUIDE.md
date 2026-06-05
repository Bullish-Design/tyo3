# Phase 2 Implementation Guide — HEAD db over OverlaySystem

> Audience: an engineer who has just finished **Phase 1**
> (`PHASE_1_IMPLEMENTATION_GUIDE.md`) and is now implementing **Phase 2** of
> `MVCC_SUBSTRATE_ARCHITECTURE.md`.
>
> Goal of Phase 2: make the **live HEAD database** a `ProjectDatabase` over the
> `OverlaySystem` built in Phase 1, constructed via real ty project **discovery**
> (`ProjectMetadata::discover` + `apply_configuration_files` +
> `ProjectDatabase::fallible`) instead of the current hand-rolled
> `ProjectMetadata::new` + `OsSystem` + `use_defaults`. Keep a `ContentStore` and
> the `OverlaySystem` handle next to the db so Phase 3 can publish edits.
>
> **Still out of scope:** edits, revisions, `apply_changes`, `SyncDelta`,
> frozen MVCC snapshots, the graph. Those are Phases 3–7. Phase 2 only rewires
> *construction* of the head and proves config discovery now works.
>
> When you finish: the project compiles, the **entire existing Python suite still
> passes unchanged**, project configuration (`pyproject.toml` / `ty.toml`) is now
> honoured, and the head carries a (currently idle) `ContentStore` + overlay
> handle wired for Phase 3.

---

## 0. What Phase 2 changes, in one paragraph

Today `PyTyProject::open` and `reload` (`rust/src/project.rs:703`, `:739`) build
the db like this:

```rust
let system = OsSystem::new(system_root.clone());
let metadata = ProjectMetadata::new(Name::new("tyo3-project"), system_root.clone());
let db = ProjectDatabase::use_defaults(metadata, system);
```

That has **two** problems the architecture removes:

1. **It ignores project configuration.** `ProjectMetadata::new` fabricates a
   blank project; it never reads the `pyproject.toml` / `ty.toml` that defines the
   Python version, include/exclude globs, rule levels, etc. ty's language server
   does *not* do this — it calls `ProjectMetadata::discover`.
2. **It builds over `OsSystem`,** so there is nowhere to put overlay content. MVCC
   and edits are impossible without the overlay layer underneath the db.

Phase 2 replaces both with the canonical ty construction path — the exact shape
ty_server uses in `crates/ty_server/src/session.rs:602-648` — but over our
`OverlaySystem` instead of `LSPSystem`, and keeps the `ContentStore` beside it.

### Reference implementation to copy

Open and read **`crates/ty_server/src/session.rs`, lines ~590–650** in the
vendored checkout
(`~/.cargo/git/checkouts/ruff-b18f69e2b025fac7/3cb09eb/`). That block is
*precisely* the construction logic you are porting:

```rust
let metadata = ProjectMetadata::discover(&root, &system);          // 1. discover
let project = metadata
    .and_then(|mut metadata| {
        metadata.apply_configuration_files(&system)?;              // 2. user config
        ProjectDatabase::fallible(metadata, system.clone())        // 3. fallible build
    });
let (root, db) = match project {
    Ok(db) => (root, db),
    Err(err) => {                                                  // 4. fallback
        let Ok(metadata) = ProjectMetadata::from_options(Options::default(), root, None, &UseDefaultStrategy);
        let db = ProjectDatabase::use_defaults(metadata, system);
        ...
    }
};
```

We mirror this verbatim, swapping `LSPSystem` → `OverlaySystem`, dropping the
LSP-specific `config_file_override` / `apply_overrides` branches we don't have,
and simplifying the fallback to `ProjectMetadata::new` (we have no `Options`
import and don't need one).

### The signatures you are calling (confirmed against rev `3cb09eb`)

- `ProjectMetadata::discover(path: &SystemPath, system: &dyn System) -> Result<ProjectMetadata, ProjectMetadataError>`
  (`ty_project/src/metadata.rs:144`).
- `ProjectMetadata::apply_configuration_files(&mut self, system: &dyn System) -> Result<(), ConfigurationFileError>`
  (`metadata.rs:318`).
- `ProjectDatabase::fallible<S>(metadata, system) -> anyhow::Result<Self>` where
  `S: System + 'static + Send + Sync + RefUnwindSafe` (`ty_project/src/db.rs:62`).
- `ProjectDatabase::use_defaults<S>(metadata, system) -> Self` (`db.rs:70`).
- `ProjectMetadata::new(name: Name, root: SystemPathBuf) -> ProjectMetadata`
  (`metadata.rs:48`) — the fallback constructor (already used today).

`OverlaySystem` satisfies the `S` bound (Phase 1 ensured `Send + Sync +
RefUnwindSafe`), and it is `Clone` where cloning **shares** the content cell
(`Arc<ArcSwap<…>>`). That sharing is load-bearing: the clone we hand to
`fallible`/`use_defaults` and the handle we keep in the head observe the *same*
overlay content, so a Phase-3 `publish()` on our handle is visible to the db.

---

## 1. Prerequisite check

Phase 2 assumes Phase 1 is merged: `rust/src/content.rs` and `rust/src/overlay.rs`
exist and their tests pass. Specifically you rely on:

- `crate::content::{ContentStore, Generation, Revision}`
- `crate::overlay::OverlaySystem` with `OverlaySystem::live(root, generation)` and
  the `Clone`-shares-content semantics from Phase 1 §7.6.

If `rust/src/overlay.rs` defines the throwaway `open_overlay_database` helper from
Phase 1 §3, you will **replace** its body/role in this phase (or delete it — see
§4). It was only ever a Phase-1 test scaffold.

Run the baseline before you touch anything, so you know the starting state is
green (see `MEMORY.md` — always via devenv, never bare cargo/pytest):

```bash
devenv shell -- check-rust
devenv shell -- test-rust
devenv shell -- tests
```

---

## 2. Restructure the head state (the only structural change)

Today there is one struct, `TyProjectState { db, root }`, and it is used for
**both** the live head (`PyTyProject.inner`) *and* the cheap per-read clone *and*
the snapshot (`PySnapshot.inner`). Phase 2 needs the head to additionally own a
`ContentStore` and an `OverlaySystem` handle — but those must **not** be cloned
on every read, and must **not** leak into `PySnapshot`.

So split the one struct into two:

- **`HeadState`** — the live head. Owns `db`, `root`, **`store`**, **`system`**.
  Lives behind `PyTyProject.inner: Mutex<Option<HeadState>>`.
- **`TyProjectState`** — unchanged `{ db, root }`. The cheap, `Send`/`Ungil`
  read clone produced for every analysis call, and what `PySnapshot` holds.
  Keep the existing name so all `compute_*` functions and `PySnapshot` are
  untouched.

### 2.1 The structs

In `rust/src/project.rs`, keep `TyProjectState` exactly as is, and add:

```rust
use crate::content::ContentStore;
use crate::overlay::OverlaySystem;

/// The live, mutable HEAD of a session. Owns the database plus the content
/// substrate behind it. Distinct from `TyProjectState` (the cheap read clone)
/// because `store`/`system` must never be cloned per-read nor exposed to
/// snapshots.
///
/// In Phase 2 `store`/`system` are wired but idle: no edits flow through them
/// yet. Phase 3 activates them (`store.insert_text` → `system.publish` →
/// `db.apply_changes`).
struct HeadState {
    db: ProjectDatabase,
    root: SystemPathBuf,
    store: ContentStore,
    /// Handle onto the *same* overlay content cell the `db` reads through
    /// (clone-shares the inner `Arc<ArcSwap<…>>`). Used by Phase 3 to publish.
    system: OverlaySystem,
}
```

`PyTyProject` becomes:

```rust
#[pyclass(name = "TyProject", module = "tyo3._native_impl", frozen)]
pub struct PyTyProject {
    inner: Mutex<Option<HeadState>>,
}
```

`PySnapshot` is **unchanged** (`Mutex<Option<TyProjectState>>`).

> Phase-2 dead-code note: `store` and `system` are written at construction and
> read by `reload` (§5, via `store.capture()`), but `system` is not otherwise
> consulted until Phase 3. If `check-rust`/clippy flags `system` as never-read,
> add `#[allow(dead_code)]` on the field with a `// activated in Phase 3` comment
> rather than deleting it — Phase 3 needs the handle. Do **not** silence it by
> faking a use.

### 2.2 Make the lock helpers generic (zero churn at call sites)

`lock_state` and `clone_locked_state` are currently typed to
`Mutex<Option<TyProjectState>>`. Both `PyTyProject` (now `HeadState`) and
`PySnapshot` (still `TyProjectState`) call them. Rather than fork them, make them
generic over a tiny trait that yields the read clone. This keeps **every existing
call site** (`clone_locked_state(&self.inner, "x")`, ~20 of them across both
impls) compiling unchanged.

```rust
/// Anything that can produce the cheap, GIL-releasable read clone.
trait ReadCloneSource {
    fn read_clone(&self) -> TyProjectState;
}

impl ReadCloneSource for TyProjectState {
    fn read_clone(&self) -> TyProjectState {
        TyProjectState { db: self.db.clone(), root: self.root.clone() }
    }
}

impl ReadCloneSource for HeadState {
    fn read_clone(&self) -> TyProjectState {
        // Clone *only* db + root. store/system stay in the head; the read clone
        // (and any snapshot built from it) never sees them.
        TyProjectState { db: self.db.clone(), root: self.root.clone() }
    }
}
```

Then generalise the two helpers (bodies otherwise identical to today):

```rust
fn lock_state<'a, T>(
    inner: &'a Mutex<Option<T>>,
    op_name: &str,
) -> PyResult<std::sync::MutexGuard<'a, Option<T>>> {
    let guard = inner.lock().map_err(|e| {
        PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
    })?;
    if guard.is_none() {
        return Err(ProjectClosedError::new_err(format!(
            "Project is closed — cannot call {}()",
            op_name
        )));
    }
    Ok(guard)
}

fn clone_locked_state<T: ReadCloneSource>(
    inner: &Mutex<Option<T>>,
    op_name: &str,
) -> PyResult<TyProjectState> {
    let guard = lock_state(inner, op_name)?;
    Ok(guard.as_ref().unwrap().read_clone())
}
```

That is the whole adaptation. Every read method on both `PyTyProject` and
`PySnapshot` now compiles as-is: `clone_locked_state(&self.inner, "check")` works
whether `self.inner` holds `HeadState` or `TyProjectState`, and always returns a
`TyProjectState`.

> Why a trait instead of just two functions: it preserves the existing call sites
> verbatim, which keeps the Phase-2 diff to *construction + struct shape* and
> makes the "no behavioural change to reads" claim auditable. The duplicate read
> walls collapse in Phase 4 anyway (architecture §6.1); don't pre-optimise here.

`close()` (`project.rs:761`) locks `self.inner` directly and sets `None`; it works
unchanged against `Mutex<Option<HeadState>>` (dropping `HeadState` drops the
`ContentStore` and overlay handle — correct).

---

## 3. The head builder (the core of Phase 2)

Add one free function that performs ty discovery over a fresh overlay and returns
a fully-formed `HeadState`. `open` and `reload` both call it.

```rust
use ruff_python_ast::name::Name;

/// Build a live HEAD over an `OverlaySystem`, seeding the overlay from
/// `initial_store` (an empty store on first open; the preserved store on reload).
///
/// Construction mirrors ty_server (`ty_server/src/session.rs:602`):
///   1. discover project metadata from disk (`pyproject.toml` / `ty.toml`),
///   2. layer user-level configuration on top,
///   3. build the db with `fallible` (surfaces config errors),
///   4. on any failure, fall back to a default blank project (never panic).
fn build_head(root: SystemPathBuf, initial_store: ContentStore) -> HeadState {
    // The overlay the db reads through. The clone handed to fallible/use_defaults
    // shares this same content cell, so `system.publish(...)` (Phase 3) is visible
    // to the db.
    let system = OverlaySystem::live(root.clone(), initial_store.capture());

    // 1+2+3: discover → apply user config → build. Each step's error is mapped to
    // a string so the chain has one error type (we have no `anyhow` dependency).
    let built: Result<ProjectDatabase, String> = ProjectMetadata::discover(&root, &system)
        .map_err(|e| format!("project discovery failed: {e}"))
        .and_then(|mut metadata| {
            metadata
                .apply_configuration_files(&system)
                .map_err(|e| format!("failed to apply configuration files: {e}"))?;
            ProjectDatabase::fallible(metadata, system.clone())
                .map_err(|e| format!("failed to build project database: {e:#}"))
        });

    let db = match built {
        Ok(db) => db,
        Err(err) => {
            // 4. Fallback: blank project over the same overlay, defaults substituted.
            tracing::warn!("{err}. Falling back to default project settings.");
            let metadata = ProjectMetadata::new(Name::new("tyo3-project"), root.clone());
            ProjectDatabase::use_defaults(metadata, system.clone())
        }
    };

    HeadState { db, root, store: initial_store, system }
}
```

Notes:

- **`system.clone()` into the builder, keep `system` in the head.** Both share the
  content cell. `OverlaySystem::live` was defined in Phase 1; if you find you need
  the *generation* rather than the system, recall `initial_store.capture()` is the
  `Generation` and `OverlaySystem::live(root, generation)` is the constructor.
- **`discover` needs `root` to be a directory** (`metadata.rs:150` returns
  `NotADirectory` otherwise). `open` already canonicalises `root` to an existing
  absolute dir, so this holds. `discover` reads `pyproject.toml`/`ty.toml` through
  the overlay — for the empty Phase-2 store that transparently falls through to
  disk via the native `OsSystem`, exactly like reading from disk directly.
- **No `anyhow`.** ty_server uses `.context(...)`; we don't depend on `anyhow`, so
  map each error to `String`. `ProjectMetadataError` and `ConfigurationFileError`
  are `thiserror` enums (`Display`), and `fallible`'s `anyhow::Error` formats with
  `{e:#}`. If you'd rather add `anyhow` to mirror ty_server one-for-one, that's a
  fine alternative — but it's optional and not required by later phases.
- **`tracing::warn!`** — confirm `tracing` is already a dependency (it is used
  throughout ty). If the crate doesn't import it, drop the line or use `eprintln!`;
  the fallback behaviour is what matters, not the log.
- **Never panics on bad config.** `fallible` surfaces misconfiguration as `Err`;
  we catch it and fall back to defaults. This matches today's effective behaviour
  (`use_defaults` never failed) so no existing test that opens a
  weirdly-configured fixture regresses.

---

## 4. Rewire `open`

Replace the body of `PyTyProject::open` (`project.rs:703-730`). Keep the existing
root canonicalisation verbatim; swap only the construction tail.

```rust
#[staticmethod]
fn open(root: &str) -> PyResult<Self> {
    let root_path = PathBuf::from(root);
    let absolute = root_path.canonicalize().map_err(|e| {
        PathResolutionError::new_err(format!("Cannot resolve root '{}': {}", root, e))
    })?;
    let s = absolute.to_str().ok_or_else(|| {
        PathResolutionError::new_err(format!(
            "Path '{}' contains non-UTF-8 characters",
            absolute.display()
        ))
    })?;
    let system_root = SystemPathBuf::from(s);

    let head = build_head(system_root, ContentStore::new());

    Ok(PyTyProject {
        inner: Mutex::new(Some(head)),
    })
}
```

Then clean up imports at the top of `project.rs`:

- Remove `OsSystem` from the `ruff_db::system::{OsSystem, SystemPathBuf}` import
  (it's now used only inside `OverlaySystem`, in `overlay.rs`). Keep
  `SystemPathBuf`.
- Keep `use ty_project::{ProjectDatabase, ProjectMetadata};`.
- Add `use ruff_python_ast::name::Name;` (or keep using the fully-qualified
  `ruff_python_ast::name::Name::new(...)` as the old code did — either is fine).
- Add `use crate::content::ContentStore;` and `use crate::overlay::OverlaySystem;`.

If Phase 1 left an `open_overlay_database` helper in `overlay.rs`, it is now
superseded by `build_head`. Either delete it, or—if its Rust tests reference
it—leave it but stop using it from `project.rs`. Prefer deleting it and porting
any useful assertion into the Phase-2 tests (§7).

---

## 5. Rewire `reload`

`reload` (`project.rs:739-755`) currently rebuilds a default db over `OsSystem`.
In the MVCC model `reload` is the seed of `sync_all` (architecture §10.2), but the
real `sync_all` — a `ChangeEvent::Rescan` through `apply_changes` — is **Phase 3**.

For Phase 2, keep the method name `reload` (renaming to `sync_all` is a Python API
change that would touch the existing test suite — defer it to Phase 3) and give it
the cleanest forward-compatible behaviour: **rebuild the head over a fresh overlay
that preserves the current store's content, re-running discovery** (so config
edits on disk are picked up). With the empty Phase-2 store this is observationally
identical to today's "drop and re-create", but it already does the right thing
once edits exist.

```rust
fn reload(&self) -> PyResult<()> {
    let mut guard = lock_state(&self.inner, "reload")?;
    let head = guard.as_mut().unwrap();

    let root = head.root.clone();
    // Preserve overlay content across the rebuild: move the existing store out and
    // re-seed the new head from it. (Empty in Phase 2; meaningful once edits land.)
    let store = std::mem::replace(&mut head.store, ContentStore::new());

    // Rebuild while still holding the lock (CPU-bound, no contention risk), then
    // swap atomically — old db/system/store drop when the guard's old value drops.
    *guard = Some(build_head(root, store));
    Ok(())
}
```

> Why `std::mem::replace` rather than rebuilding from scratch: it carries the
> `ContentStore` (and thus any overlay buffers, in Phase 3) across the reload while
> still producing a brand-new `ProjectDatabase` and `OverlaySystem`. Building a new
> head from `ContentStore::new()` instead would silently drop overlay edits on
> every reload — wrong once Phase 3 lands, so do it right now.
>
> If you prefer the literal "reload = forget everything, re-read disk" semantics
> for Phase 2, pass `ContentStore::new()` instead of the preserved `store`. Either
> passes the existing suite (the store is empty), but the preserving form is the
> one Phase 3 wants — use it.

---

## 6. Leave reads, `snapshot`, and `PySnapshot` alone

Do **not** touch:

- Any `compute_*` analysis function — they take `&TyProjectState` and are unchanged.
- Any read wrapper on `PyTyProject` or `PySnapshot` — the generic
  `clone_locked_state` (§2.2) keeps them compiling and behaving identically.
- `snapshot()` (`project.rs:776`) — it still calls
  `clone_locked_state(&self.inner, "snapshot")` (now via `HeadState::read_clone`),
  still eagerly materialises `source_text` for every project file, still returns a
  `PySnapshot` over a cloned `TyProjectState`. The eager-materialisation hack and
  the duplicated read walls are deliberately retained; they are removed in
  **Phase 4**, not here.
- `close()` — works unchanged against `Mutex<Option<HeadState>>`.

The point of Phase 2 is that this is a *construction* change, not a *behaviour*
change for reads. Resist the urge to also do Phase 4's cleanup.

---

## 7. Tests

### 7.1 Rust unit tests (in `project.rs` `#[cfg(test)]`, or `overlay.rs`)

These prove the new construction path. Reuse the `tempfile` dev-dependency added
in Phase 1.

```rust
#[cfg(test)]
mod phase2_tests {
    use super::*;
    use std::io::Write;

    /// Temp project dir with `a.py` and an optional `pyproject.toml`.
    fn project(pyproject: Option<&str>, a_py: &str) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        if let Some(toml) = pyproject {
            let mut f = std::fs::File::create(dir.path().join("pyproject.toml")).unwrap();
            f.write_all(toml.as_bytes()).unwrap();
        }
        let mut f = std::fs::File::create(dir.path().join("a.py")).unwrap();
        f.write_all(a_py.as_bytes()).unwrap();
        let root = SystemPathBuf::from_path_buf(
            dir.path().canonicalize().unwrap().to_path_buf()
        ).unwrap();
        (dir, root)
    }

    /// build_head over a directory with no config still yields a working db that
    /// reads disk content through the overlay.
    #[test]
    fn build_head_no_config_reads_disk() {
        use ruff_db::source::source_text;
        let (_dir, root) = project(None, "VALUE = 42\n");
        let head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");
        let file = ruff_db::files::system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, file).as_str().contains("VALUE = 42"));
    }

    /// THE Phase-2 behavioural win: a `pyproject.toml` is discovered and applied.
    /// With no config, `check()` reports the diagnostic; with the rule disabled in
    /// config, it does not. (Verify the exact rule name against ty — see note.)
    #[test]
    fn config_discovery_is_applied() {
        // Code that triggers some default-on diagnostic. Pick a rule you can
        // confirm fires by default for this snippet, then disable it by name.
        let offending = "import os\n"; // e.g. an unused-import style rule
        let toml = "[tool.ty.rules]\nunused-import = \"ignore\"\n";

        let (_d1, root_on) = project(None, offending);
        let head_on = build_head(root_on, ContentStore::new());
        let diags_on = head_on.db.check();

        let (_d2, root_off) = project(Some(toml), offending);
        let head_off = build_head(root_off, ContentStore::new());
        let diags_off = head_off.db.check();

        assert!(
            diags_off.len() < diags_on.len(),
            "disabling a rule in pyproject.toml must reduce diagnostics \
             (on={}, off={}) — proves discovery+apply_configuration_files ran",
            diags_on.len(), diags_off.len(),
        );
    }

    /// Malformed config must not panic: build_head falls back to defaults.
    #[test]
    fn malformed_config_falls_back_to_defaults() {
        let (_dir, root) = project(Some("this is not = valid toml ]["), "X = 1\n");
        let head = build_head(root.clone(), ContentStore::new()); // must not panic
        // The db is usable despite the broken config.
        let a = root.join("a.py");
        let file = ruff_db::files::system_path_to_file(&head.db, &a).unwrap();
        use ruff_db::source::source_text;
        assert!(source_text(&head.db, file).as_str().contains("X = 1"));
    }
}
```

> **Verify rule names against ty.** `config_discovery_is_applied` is the
> load-bearing test, but the exact rule key (`unused-import`, the TOML table path
> `[tool.ty.rules]`, and whether the chosen snippet actually triggers it by
> default) must be confirmed against this ty pin. Use the running ty CLI or
> `ty_project`'s rule registry to pick a rule that (a) fires for a trivial snippet
> by default and (b) is configurable to `"ignore"`. If you cannot find a clean
> rule differential, an equally valid proof is asserting a **python-version**
> effect: set `[tool.ty.environment] python-version = "3.8"` and check that a
> 3.9+-only syntax/library diagnostic appears that is absent under defaults. The
> requirement is one assertion that *only passes if config was discovered and
> applied* — that's what distinguishes Phase 2 from the old `ProjectMetadata::new`
> path.

### 7.2 Python regression + behavioural test

The primary Python deliverable is **no regressions**: every existing test in
`src/tyo3/tests/` must still pass, because `open`/`reload`/reads are
observationally unchanged for projects without `[tool.ty]` config (most fixtures).
Run the full suite (§8).

Add **one** new Python test that proves config discovery end-to-end through the
binding (mirror of the Rust test, but via the public API). Sketch:

```python
# src/tyo3/tests/test_config_discovery.py
import textwrap
from tyo3 import TyProject  # match the existing import style in the suite

def test_pyproject_rule_config_is_honored(tmp_path):
    (tmp_path / "a.py").write_text("import os\n")
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""\
        [tool.ty.rules]
        unused-import = "ignore"
    """))
    proj = TyProject.open(str(tmp_path))
    result = proj.check()
    # Assert the suppressed diagnostic is absent. Shape the assertion to the
    # actual check() return type used elsewhere in the suite.
    assert all("unused" not in str(d).lower() for d in _iter_diags(result))
    proj.close()
```

Match the import path, the `check()` return shape, and diagnostic access pattern
to whatever the existing tests (e.g. `test_rust_integration.py`,
`test_lsp_features.py`) already use — don't invent a new convention. Keep the rule
name in sync with the Rust test.

---

## 8. Build, test, iterate

```bash
devenv shell -- check-rust     # type/borrow check while iterating
devenv shell -- test-rust      # Phase-1 + Phase-2 Rust tests
devenv shell -- tests          # FULL Python suite — the regression guard
```

The full Python suite passing unchanged is the central proof that Phase 2 rewired
construction without altering observable read behaviour.

---

## 9. Gotchas & decisions (read before you debug)

1. **`OverlaySystem` must be `Clone` with shared content.** `build_head` clones
   `system` into the db builder and keeps the original in `HeadState`. If your
   Phase-1 `Clone` deep-copied the content (it should not — Phase 1 §7.6), the
   db and the head handle would diverge and Phase 3's `publish` would silently do
   nothing. Confirm the clone shares the inner `Arc<ArcSwap<…>>`.

2. **`discover` reads config *through the overlay*, not raw disk.** That's
   intentional — in Phase 3 an agent can overlay a `pyproject.toml` and have
   discovery honour the hypothetical config. In Phase 2 the store is empty so it
   falls through to `OsSystem`; behaviour is identical to reading disk. Don't
   "optimise" by passing the native system to `discover`.

3. **Two error types in the discovery chain.** `discover` →
   `ProjectMetadataError`, `apply_configuration_files` → `ConfigurationFileError`,
   `fallible` → `anyhow::Error`. They don't unify automatically. The guide maps
   each to `String` (§3); if you add `anyhow` instead, use `.context(...)` exactly
   like ty_server. Don't try to make them one enum.

4. **`ProjectDatabase::fallible` requires `'static + Send + Sync + RefUnwindSafe`
   on the system.** Already satisfied by `OverlaySystem` (Phase 1). If you see a
   bound error here, the regression is in `overlay.rs`, not this phase.

5. **Don't rename `reload` to `sync_all` yet.** The Python method name is part of
   the existing test surface. Phase 3 introduces `sync_all` (real rescan via
   `apply_changes`) and can alias/redefine `reload` then. Renaming now creates
   churn and breaks the regression guard for no Phase-2 benefit.

6. **`HeadState.system` may warn as unused in Phase 2.** It's written and (via
   reload's `store.capture()` path) the store is used, but the `system` field
   isn't read until Phase 3. Prefer `#[allow(dead_code)]` + a `// Phase 3` comment
   over deleting it. (`store` is read by `reload`'s `mem::replace`, so it should
   not warn.)

7. **`root` canonicalisation stays in `open`.** `discover` requires a real
   directory; the existing `canonicalize()` guarantees it. Don't move that logic
   into `build_head` (reload already has a canonical `root`).

---

## 10. Explicitly OUT of scope for Phase 2

- `edit` / `edit_virtual` / `sync_path` / `sync_all` and any mutation through the
  store or `system.publish` (Phase 3).
- `apply_changes`, `ChangeEvent` synthesis, `SyncDelta`, the `Revision` API
  surfaced to Python (Phase 3).
- Frozen MVCC snapshots (`OverlaySystem::frozen`), collapsing the duplicated read
  walls, deleting the eager-materialisation hack in `snapshot()` (Phase 4).
- Any graph change (Phases 6–7), the watcher (Phase 8), the floating
  `session.check()` fast path (Phase 9).

If you find yourself writing `apply_changes`, adding an `edit` method, or building
a `frozen` system, stop — you've left Phase 2.

---

## 11. Definition of Done

- [ ] `HeadState { db, root, store, system }` added; `PyTyProject.inner` is
      `Mutex<Option<HeadState>>`; `PySnapshot` unchanged.
- [ ] `ReadCloneSource` trait + generic `lock_state` / `clone_locked_state`; all
      existing read call sites compile unchanged.
- [ ] `build_head(root, store)` performs discover → `apply_configuration_files`
      → `fallible`, with a `ProjectMetadata::new` + `use_defaults` fallback that
      never panics.
- [ ] `open` and `reload` rewired onto `build_head`; `reload` preserves the store
      via `mem::replace`.
- [ ] `OsSystem` import removed from `project.rs`; `ContentStore`/`OverlaySystem`
      imported; Phase-1 `open_overlay_database` scaffold removed (or no longer
      referenced).
- [ ] Rust tests pass: `build_head_no_config_reads_disk`,
      `config_discovery_is_applied` (or the python-version variant),
      `malformed_config_falls_back_to_defaults`.
- [ ] New Python test `test_config_discovery` passes.
- [ ] `devenv shell -- tests` shows **no regressions** in the existing suite.
- [ ] `devenv shell -- check-rust` clean.
- [ ] PR description notes any signature deviations from this guide and the exact
      rule/version used in the discovery test (so the guide can be corrected).

---

## 12. How this seeds Phase 3

You now have a head that owns the substrate:

- **Phase 3** adds `edit`/`edit_virtual`/`sync_path`/`sync_all` on `PyTyProject`.
  Each locks the head, mutates `head.store` (e.g. `store.insert_text(path, text)`),
  republishes via `head.system.publish(store.capture())`, synthesises the precise
  `Vec<ChangeEvent>`, calls `head.db.apply_changes(&events, None)`, bumps the
  `Revision`, and returns a `SyncDelta`/`SyncResult`. Because `system` and the
  db's internal system share the content cell (§9.1), `publish` is immediately
  visible to `apply_changes`.
- `reload` becomes the `sync_all` rescan (`ChangeEvent::Rescan` through
  `apply_changes`) instead of a full db rebuild; the `build_head` rebuild remains
  the cold-start / hard-reset path.
- **Phase 4** uses `head.store.capture()` + `OverlaySystem::frozen(root, gen, rev)`
  to build the independent, never-cancelled MVCC snapshot databases, and finally
  collapses the duplicated read walls into the single snapshot read surface.

Keep the construction path clean and the regression guard green, and Phase 3 drops
straight onto `head.store` / `head.system`.
