//! Thin PyO3 method wrappers for `PyTyProject` (parse args, call core, convert errors).
//!
//! Split from `project.rs` (Phase 13). Pulls shared imports + state
//! types from the parent module via `use super::*`.

use super::*;

// ── PyO3 Methods ─────────────────────────────────────────────────────────

#[pymethods]
impl PyTyProject {
    /// Open a project at the given root directory.
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

        let sidecar = Sidecar::new(&absolute);
        let raw_config = RawConfig::load(&sidecar).map_err(config_error_to_pyerr)?;
        let config = config::validate(raw_config).map_err(config_error_to_pyerr)?;
        if sidecar.exists() {
            if config.raw.sidecar.gitignore_cache {
                sidecar.ensure_gitignore_cache_policy().map_err(|e| {
                    PyConfigError::new_err(format!("failed to update sidecar .gitignore: {e}"))
                })?;
            }
            sidecar.ensure_layout_marker().map_err(|e| {
                PyConfigError::new_err(format!("failed to update sidecar layout marker: {e}"))
            })?;
        }
        let registry = match fs::read(sidecar.identity_db_path()) {
            Ok(bytes) => match IdentityRegistry::from_bytes(&bytes) {
                Ok(registry) => registry,
                Err(FormatError::UnknownVersion(version)) => {
                    return Err(FormatVersionError::new_err(format!(
                        "unknown identity.db format_version: {version}"
                    )));
                }
                Err(e) => {
                    log::warn!("Failed to load identity registry: {} — starting fresh.", e);
                    IdentityRegistry::default()
                }
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => IdentityRegistry::default(),
            Err(e) => {
                log::warn!("Failed to read identity registry: {} — starting fresh.", e);
                IdentityRegistry::default()
            }
        };

        let retain_cap = config.raw.spine.retain_cap;
        let mut store = ContentStore::with_retain_cap(retain_cap);

        // Phase 1: ingest project content into the initial revision before
        // building the live database.  This makes the generation complete so
        // no later read triggers a write, and snapshot construction can
        // build frozen views directly from the generation without disk I/O.
        //
        // Convention: the store seeds Revision(0) as empty; ingest_project
        // produces Revision(1) as the first populated generation, so the
        // initial observable head revision is 1.
        store.ingest_project(&system_root, is_project_relevant);

        let mut head = build_head_with_config(
            system_root,
            store,
            registry,
            config.clone(),
        );

        // Phase 1: reconcile identity at open so the registry is populated
        // before any read occurs.  Identity must already be resolved so a
        // later graph read never calls sync_all (Phase 4). The open ingest
        // already published the initial revision, so reconcile against the
        // current revision (not next_revision — that offset is for the
        // deferred-publish commit path).
        let open_rev = head.store.revision();
        run_identity_reconciliation(&mut head, None, open_rev);
        // Persist the reconciled registry at open so a reopen restores bindings
        // (§5.10). Previously done inside reconciliation; Phase 5 makes identity
        // persistence an explicit, propagated step.
        persist_identity(&head)?;

        // Load authored records for each declared authored layer (§11.3.2).
        head.authored = load_authored_records(&head.config, &head.sidecar)
            .map_err(|e| {
                match e {
                    AuthoredLoadError::Json(msg) => {
                        FormatVersionError::new_err(format!("authored record parse error: {msg}"))
                    }
                    AuthoredLoadError::UnknownVersion(v) => {
                        FormatVersionError::new_err(format!(
                            "unknown authored record format_version: {v}"
                        ))
                    }
                }
            })?;

        Ok(PyTyProject {
            inner: Arc::new(Mutex::new(Some(head))),
            pending: Arc::new(Mutex::new(Vec::new())),
            watcher: Mutex::new(None),
        })
    }

    fn config_json(&self) -> PyResult<String> {
        let guard = lock_state(&self.inner, "config_json")?;
        let head = guard.as_ref().unwrap();
        serde_json::to_string(&head.config)
            .map_err(|e| PyConfigError::new_err(format!("failed to serialise config: {e}")))
    }

    // ── Lifecycle: Reload ────────────────────────────────────────────

    /// Reload the project: drop the current database and re-create it.
    /// Clears all cached diagnostics and symbol data. Preserves any overlay
    /// content across the rebuild so Phase 3 edits survive a reload.
    ///
    /// Holds the lock throughout — no window where concurrent callers
    /// see a closed project.
    fn reload(&self) -> PyResult<()> {
        let mut guard = lock_state(&self.inner, "reload")?;
        let head = guard.as_mut().unwrap();

        let root = head.root.clone();
        // Preserve overlay content, identity registry, and validated config across the rebuild.
        let store = std::mem::take(&mut head.store);
        let registry = std::mem::take(&mut head.registry);
        let config = head.config.clone();

        // Rebuild while still holding the lock, then swap atomically.
        *guard = Some(build_head_with_config(root, store, registry, config));
        drop(guard);

        // If a watcher is running, update its watched paths (the head db
        // changed). If update() errors, we leave the old watch set — fine for
        // a same-root reload where paths are identical.
        let mut w = self.watcher.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("watcher lock poisoned: {e}"))
        })?;
        if let Some(ref mut pw) = *w {
            let inner_guard = lock_state(&self.inner, "reload")?;
            pw.update(&inner_guard.as_ref().unwrap().db);
        }
        Ok(())
    }

    // ── Lifecycle: Close ─────────────────────────────────────────────

    /// Close the project and free all resources.
    /// Idempotent — closing an already-closed project is a no-op.
    fn close(&self) -> PyResult<()> {
        // Stop the watcher first so its threads don't outlive the project.
        let mut w = self.watcher.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("watcher lock poisoned: {e}"))
        })?;
        if let Some(pw) = w.take() {
            pw.stop();
        }
        drop(w);

        let mut guard = self.inner.lock().map_err(|_e| {
            PyRuntimeError::new_err("Lock poisoned".to_string())
        })?;

        // Setting None on an already-None guard is harmless.
        *guard = None;
        Ok(())
    }

    // ── Write path ─────────────────────────────────────────────────
    //
    // All writes hold the GIL and mutate the HeadState in-place.
    // Each returns a SyncResult dict (via pythonize) describing the delta.
    //
    // ARCHITECTURE: HEAD is mutated in place; snapshots are independent
    // frozen databases that never block or are blocked by writes.  Every
    // write advances the application revision, records content in the
    // generation, and publishes the new generation so a reader observing
    // revision R sees consistent content (§3.3.3).

    /// Overlay `path` with in-memory `text` (no disk write). Returns a
    /// SyncResult dict with the new revision and the affected paths.
    fn edit<'py>(&self, py: Python<'py>, path: &str, text: &str) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "edit")?;
            let head = guard.as_mut().unwrap();
            let abs = resolve_sync_path(&head.root, path);
            // Classify against the pre-edit content BEFORE the staged mutation.
            let event = classify_overlay_edit(&head.system, &head.db, &abs);
            let path_str = abs.as_str().to_string();
            let (created, changed) = match &event {
                ChangeEvent::Created { .. } => (vec![path_str], vec![]),
                _ => (vec![], vec![path_str]),
            };
            let changes = vec![crate::content::Change::Insert {
                path: abs.clone(),
                text: Arc::from(text),
            }];
            let mutation = Mutation::Overlay {
                changes,
                events: vec![event],
                created,
                changed,
                unsaved: vec![abs],
            };
            commit(head, mutation)?
        };
        commit_dto_to_py(py, dto)
    }

    /// Overlay many files atomically (one publish, one `apply_changes`, one
    /// published revision).
    fn edit_many<'py>(
        &self,
        py: Python<'py>,
        edits: std::collections::HashMap<String, String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "edit_many")?;
            let head = guard.as_mut().unwrap();

            // Build one Vec<Change> and one Vec<ChangeEvent> classified against
            // the pre-edit content — one revision for the entire multi-edit.
            let mut changes = Vec::with_capacity(edits.len());
            let mut events = Vec::with_capacity(edits.len());
            let (mut created, mut changed) = (Vec::new(), Vec::new());
            let mut unsaved = Vec::with_capacity(edits.len());
            for (path, text) in edits {
                let abs = resolve_sync_path(&head.root, &path);
                let event = classify_overlay_edit(&head.system, &head.db, &abs);
                changes.push(crate::content::Change::Insert {
                    path: abs.clone(),
                    text: Arc::from(text),
                });
                match &event {
                    ChangeEvent::Created { .. } => created.push(abs.as_str().to_string()),
                    _ => changed.push(abs.as_str().to_string()),
                }
                unsaved.push(abs);
                events.push(event);
            }
            let mutation = Mutation::Overlay { changes, events, created, changed, unsaved };
            commit(head, mutation)?
        };
        commit_dto_to_py(py, dto)
    }

    /// Overlay a virtual/unsaved buffer (e.g. "untitled:1"). No disk involvement.
    fn edit_virtual<'py>(
        &self,
        py: Python<'py>,
        uri: &str,
        text: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "edit_virtual")?;
            let head = guard.as_mut().unwrap();

            let vpath = SystemVirtualPathBuf::from(uri.to_string());
            let is_new = head.db.files().try_virtual_file(&vpath).is_none();
            let event = if is_new {
                ChangeEvent::CreatedVirtual(vpath.clone())
            } else {
                ChangeEvent::ChangedVirtual(vpath.clone())
            };
            let (created, changed) = if is_new {
                (vec![uri.to_string()], vec![])
            } else {
                (vec![], vec![uri.to_string()])
            };
            let changes = vec![crate::content::Change::InsertVirtual {
                path: vpath,
                text: Arc::from(text),
            }];
            // Virtual buffers are never watched, so they do not join the
            // unsaved-overlay (buffer-wins) set.
            let mutation = Mutation::Overlay {
                changes,
                events: vec![event],
                created,
                changed,
                unsaved: vec![],
            };
            commit(head, mutation)?
        };
        commit_dto_to_py(py, dto)
    }

    /// Author (write) an authored value for `(layer, durable_id)`.
    ///
    /// A real revision-producing commit funnelled through `commit`: it validates
    /// the layer/value/id, stages the copy-on-write `AuthoredStore` and serialises
    /// the record, persists it crash-safely as a staged step, and publishes the
    /// revision LAST (§5.3 stage → persist → publish). A persistence failure rolls
    /// the whole commit back — head included — via the staged-commit machinery; no
    /// in-memory-only `prior` rollback remains.
    fn author<'py>(
        &self,
        py: Python<'py>,
        layer: &str,
        id: &str,
        value_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "author")?;
            let head = guard.as_mut().unwrap();
            // Parse value JSON early (before any staging); layer/id validation
            // happens in `build_plan`, still before any state moves.
            let value: serde_json::Value = serde_json::from_str(value_json).map_err(|e| {
                PyValueError::new_err(format!("invalid authored value JSON: {e}"))
            })?;
            let mutation = Mutation::Author {
                layer: layer.to_string(),
                id: id.to_string(),
                value,
            };
            commit(head, mutation)?
        };
        commit_dto_to_py(py, dto)
    }

    /// Ingest a disk change for `path`: drop any overlay for it and re-read disk.
    fn sync_path<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "sync_path")?;
            let head = guard.as_mut().unwrap();
            let abs = resolve_sync_path(&head.root, path);
            commit(head, Mutation::SyncPath { abs })?
        };
        commit_dto_to_py(py, dto)
    }

    /// Drop the overlay buffer for `path`, reverting to disk. Same semantics as
    /// `sync_path` but named for intent.
    fn discard<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "discard")?;
            let head = guard.as_mut().unwrap();
            let abs = resolve_sync_path(&head.root, path);
            commit(head, Mutation::SyncPath { abs })?
        };
        commit_dto_to_py(py, dto)
    }

    /// Re-ingest the whole project from disk (one rescan revision). New/changed
    /// disk files are discovered (Phase 5 carry-over of the Phase 1 `sync_all`
    /// gap); unsaved overlay buffers are preserved.
    fn sync_all<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "sync_all")?;
            let head = guard.as_mut().unwrap();
            commit(head, Mutation::SyncAll)?
        };
        commit_dto_to_py(py, dto)
    }

    /// TEST-ONLY (§0.4): arm a one-shot commit fault at `stage`. The very next
    /// commit fires a typed error at that staged boundary — after the stage's
    /// work, before the publish tail — and rolls the commit back to R−1. Inert
    /// when unarmed (a `None` cell is a pure no-op). Stage names match the
    /// rollback test's `_FAULT_STAGES`: "code_layer", "identity_persist",
    /// "authored_persist". This is a deliberate, documented test seam; it never
    /// fires in normal use.
    fn _fault_inject(&self, stage: &str) -> PyResult<()> {
        let mut guard = lock_state(&self.inner, "_fault_inject")?;
        guard.as_mut().unwrap().armed_fault = Some(stage.to_string());
        Ok(())
    }

    // ── File watching (Phase 8) ────────────────────────────────────────

    /// Start observing the filesystem. Registers ty's ProjectWatcher over the
    /// head db's watched paths (project root + module search paths + config).
    /// Observed changes are debounced by ty and queued; call `poll_changes()`
    /// to fold them into HEAD. Idempotent: calling watch() again replaces the
    /// watcher.
    fn watch(&self) -> PyResult<()> {
        // Build the handler first — it only needs the queue, not the head.
        let pending = Arc::clone(&self.pending);
        let handler = move |changes: Vec<ChangeEvent>| {
            if let Ok(mut q) = pending.lock() {
                q.extend(changes);
            }
            // A poisoned queue mutex means a prior drain panicked; dropping the
            // batch is acceptable (the next Rescan/sync_all re-syncs). Never
            // panic on the watcher thread.
        };

        let raw_watcher = directory_watcher(handler)
            .map_err(|e| PyRuntimeError::new_err(format!("failed to start file watcher: {e}")))?;

        // ProjectWatcher::new needs &db to derive the watched paths.
        let project_watcher = {
            let guard = lock_state(&self.inner, "watch")?;
            let head = guard.as_ref().unwrap();
            ProjectWatcher::new(raw_watcher, &head.db)
            // guard dropped here
        };

        let mut w = self
            .watcher
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("watcher lock poisoned: {e}")))?;
        // Replace any existing watcher; the old one's threads stop on drop.
        *w = Some(project_watcher);
        Ok(())
    }

    /// Stop observing the filesystem. Pending unpolled events are discarded
    /// along with the watcher threads. No-op if not watching.
    fn unwatch(&self) -> PyResult<()> {
        let mut w = self
            .watcher
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("watcher lock poisoned: {e}")))?;
        if let Some(pw) = w.take() {
            pw.stop();
        }
        Ok(())
    }

    /// Force the debouncer to emit any pending batch now (still asynchronous —
    /// the handler runs on the watcher thread). Tests call this before
    /// poll_changes to shorten the wait; production code rarely needs it.
    fn flush_watch(&self) -> PyResult<()> {
        let w = self
            .watcher
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("watcher lock poisoned: {e}")))?;
        if let Some(pw) = w.as_ref() {
            pw.flush();
        }
        Ok(())
    }

    /// Drain all events the watcher has observed and fold them into HEAD as one
    /// revision. Returns a SyncResult dict, or None if nothing was pending (or
    /// everything was filtered out as overlaid). Single-writer: holds the head
    /// lock for the apply, exactly like edit()/sync_path().
    fn poll_changes<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        // 1. Drain the queue (separate lock; released immediately).
        let events: Vec<ChangeEvent> = {
            let mut q = self
                .pending
                .lock()
                .map_err(|e| PyRuntimeError::new_err(format!("watch queue poisoned: {e}")))?;
            std::mem::take(&mut *q)
        };

        if events.is_empty() {
            return Ok(None);
        }

        // 2. Apply under the head lock (the single-writer section) through the
        //    one commit funnel. An empty/all-filtered batch returns None — a
        //    no-op, never a published empty revision (§5.3).
        let dto = {
            let mut guard = lock_state(&self.inner, "poll_changes")?;
            let head = guard.as_mut().unwrap();
            commit(head, Mutation::Poll { events })?
            // guard dropped here
        };

        match dto {
            None => Ok(None),
            Some(dto) => {
                let obj = pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
                Ok(Some(obj))
            }
        }
    }

    /// Test/diagnostic seam: enqueue events as if the watcher had observed
    /// them. Lets the Python suite exercise poll_changes deterministically
    /// (no real FS timing). Paths are resolved like sync_path (relative →
    /// joined onto root).
    #[pyo3(signature = (changes))]
    fn _inject_changes(&self, changes: Vec<(String, String)>) -> PyResult<()> {
        let guard = lock_state(&self.inner, "_inject_changes")?;
        let root = guard.as_ref().unwrap().root.clone();
        drop(guard);

        let mut events = Vec::with_capacity(changes.len());
        for (kind, path) in changes {
            let abs = resolve_sync_path(&root, &path);
            let event = match kind.as_str() {
                "created" => ChangeEvent::Created { path: abs, kind: CreatedKind::File },
                "changed" => ChangeEvent::file_content_changed(abs),
                "deleted" => ChangeEvent::Deleted { path: abs, kind: DeletedKind::Any },
                "rescan"  => ChangeEvent::Rescan,
                other => return Err(PyValueError::new_err(format!("unknown change kind {other:?}"))),
            };
            events.push(event);
        }
        let mut q = self
            .pending
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("watch queue poisoned: {e}")))?;
        q.extend(events);
        Ok(())
    }

    /// The current application revision.
    #[getter]
    fn head(&self) -> PyResult<u64> {
        let guard = lock_state(&self.inner, "head")?;
        Ok(guard.as_ref().unwrap().store.revision().0)
    }

    /// Phase 1 test seam: the number of project-content files read from
    /// disk by ingest helpers.  Increments once per file actually read;
    /// snapshot construction (after Phase 1.4) adds zero reads, proving
    /// O(1) capture structurally.
    fn project_content_disk_reads(&self) -> PyResult<u64> {
        let guard = lock_state(&self.inner, "project_content_disk_reads")?;
        Ok(guard.as_ref().unwrap().store.disk_read_count())
    }

    // ── Snapshot ─────────────────────────────────────────────────────

    /// Pin a revision-isolated MVCC snapshot. `at=None` pins the current head
    /// revision; `at=r` time-travels to a still-retained revision (else an error).
    ///
    /// The returned snapshot owns an INDEPENDENT `ProjectDatabase` (its own
    /// `Zalsa`), so holding it across HEAD edits neither blocks the writer nor
    /// risks cancellation (architecture §0). No eager materialisation: content
    /// is pinned by the captured `Generation` + read-once disk capture.
    #[pyo3(signature = (at=None))]
    fn snapshot(&self, at: Option<u64>) -> PyResult<PySnapshot> {
        let guard = lock_state(&self.inner, "snapshot")?;
        let head = guard.as_ref().unwrap();
        let root = head.root.clone();

        let is_head = at.is_none();
        let registry = head.registry.clone();
        let authored = Some(head.authored.clone());
        let config = head.config.clone();
        let hash_policies = head.hash_policies.clone();
        let default_hash_profile = head.default_hash_profile.clone();
        let (generation, rev) = match at {
            None => (head.store.capture(), head.store.revision()),
            Some(r) => {
                let rev = Revision(r);
                let gen = head.store.generation_at(rev).ok_or_else(|| {
                    RevisionEvictedError::new_err(format!(
                        "revision {} is no longer retained (oldest retained: {})",
                        r,
                        head.store.oldest_retained().0
                    ))
                })?;
                (gen, rev)
            }
        };
        drop(guard); // release the head lock BEFORE the (cold) db build

        let mut state = build_frozen(root, generation, rev, hash_policies, default_hash_profile);
        state.registry = Some(registry);
        state.authored = authored;
        Ok(PySnapshot {
            inner: Mutex::new(Some(state)),
            revision: rev.0,
            config,
            is_head,
        })
    }

    // ── Floating warm fast path (Phase 9) ─────────────────────────────

    /// A floating, warm view of the live HEAD (architecture §6 "floating latest
    /// reads"). Each read reflects the newest revision, reuses the writer's
    /// memos, and is internally retried on salsa cancellation. For pinned,
    /// isolated reads use `snapshot()` instead.
    fn head_view(&self) -> PyResult<PyHeadView> {
        // Validate the project is open, then share the head handle.
        drop(lock_state(&self.inner, "head_view")?);
        Ok(PyHeadView {
            inner: Arc::clone(&self.inner),
        })
    }

    // ── Identity: id_for, locate (Gate 2 Step 6) ──────────────────────

    // ── Native code delta (Phase 2, parity-only) ─────────────────

    /// Produce a **full** (cold-start) native code delta for the current head
    /// state — every node upserted, every edge added, `rescan = true` — against
    /// an empty previous layer. The parity oracle applies this to a fresh
    /// `CodeGraph` and compares it to the legacy read-surface build.
    ///
    /// This is a pure read of the head state (it does not mutate the head or its
    /// stored code layer); the in-commit producer is what maintains the layer.
    fn full_code_delta<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let (state, revision) = {
            let guard = lock_state(&self.inner, "full_code_delta")?;
            let head = guard.as_ref().unwrap();
            (head.read_clone(), head.store.revision().0)
        };
        let empty = crate::code_layer::CodeLayer::new();
        let delta = py.detach(move || {
            let (_next, delta) =
                crate::code_layer::produce_code_delta(&state, &empty, revision, true, None);
            delta
        });
        pythonize(py, &delta).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Resolve the DurableId of the entity at `(path, line, col)`.
    ///
    /// Finds the enclosing symbol at the position, builds its qualified_path,
    /// and looks up the DurableId in the identity registry. Returns None if
    /// no entity was found at the position or no identity is registered.
    fn id_for(&self, path: &str, line: u32, col: u32) -> PyResult<Option<String>> {
        let guard = lock_state(&self.inner, "id_for")?;
        let head = guard.as_ref().unwrap();

        let state = TyProjectState {
            db: head.db.clone(),
            root: head.root.clone(),
            registry: Some(head.registry.clone()),
            hash_policy: head.hash_policy,
            hash_policies: head.hash_policies.clone(),
            default_hash_profile: head.default_hash_profile.clone(),
            authored: None,
        };

        // Resolve the file.
        let file = crate::files::resolve_file(
            &state.db,
            state.root.as_std_path(),
            path,
        ).map_err(|e| PathResolutionError::new_err(e.to_string()))?;

        let file_path = file.path(&state.db).as_str().to_string();

        // Get document symbols.
        let flat_symbols = ty_ide::document_symbols(&state.db, file);
        let hierarchical = flat_symbols.to_hierarchical();

        let src = ruff_db::source::source_text(&state.db, file);
        let source_str = src.as_str();
        let line_index = ruff_source_file::LineIndex::from_source_text(source_str);

        // Convert position to offset.
        let offset = crate::coordinates::position_to_offset_with_index(
            source_str, &line_index, line, col,
        ).map_err(|e| PositionError::new_err(e.to_string()))?;

        // Find all symbols that contain this offset, pick the innermost.
        let mut best: Option<(String, u32)> = None; // (qualified_path, range_size)

        // Walk all symbols via iter() and figure out containment + nesting.
        for (id, info) in hierarchical.iter() {
            if info.full_range.contains(offset) {
                // Build qualified_path by walking up the hierarchy.
                let qp = build_qualified_path(&hierarchical, id, &file_path);
                let size = info.full_range.len().to_u32();
                match &best {
                    None => best = Some((qp, size)),
                    Some((_, prev_size)) if size < *prev_size => {
                        best = Some((qp, size));
                    }
                    _ => {}
                }
            }
        }

        // Look up in the registry.
        match best {
            Some((qp, _)) => Ok(head.registry.by_path(&qp).map(|id| id.0.clone())),
            None => Ok(None),
        }
    }

    /// Locate the current file+qualified_path for a DurableId.
    ///
    /// Returns None if the id is not in the registry (e.g. retired, or
    /// from another session).
    fn locate(&self, durable_id: &str) -> PyResult<Option<String>> {
        let guard = lock_state(&self.inner, "locate")?;
        let head = guard.as_ref().unwrap();
        let id = DurableId(durable_id.to_string());
        Ok(head.registry.get(&id).map(|a| a.qualified_path.clone()))
    }

    /// List DurableIds currently flagged as NeedsReview.
    fn needs_review(&self) -> PyResult<Vec<String>> {
        let guard = lock_state(&self.inner, "needs_review")?;
        let head = guard.as_ref().unwrap();
        Ok(head.registry.iter()
            .filter(|a| a.status == crate::identity::IdentityStatus::NeedsReview)
            .map(|a| a.id.0.clone())
            .collect())
    }

    /// List DurableIds currently flagged as Orphaned.
    fn orphaned(&self) -> PyResult<Vec<String>> {
        let guard = lock_state(&self.inner, "orphaned")?;
        let head = guard.as_ref().unwrap();
        Ok(head.registry.iter()
            .filter(|a| a.status == crate::identity::IdentityStatus::Orphaned)
            .map(|a| a.id.0.clone())
            .collect())
    }
}


/// Build a qualified_path for a SymbolId by walking up the hierarchy.
pub(crate) fn build_qualified_path(
    hierarchical: &ty_ide::HierarchicalSymbols,
    target_id: ty_ide::SymbolId,
    file_path: &str,
) -> String {
    // Walk the hierarchy upward: collect name segments.
    let mut segments: Vec<String> = Vec::new();

    // Get the symbol info for the target.
    for (id, info) in hierarchical.iter() {
        if id == target_id {
            segments.push(info.name.clone().into_owned());
            let mut current_id = id;

            // Walk up: for each symbol, check if any id has it as a child.
            // This is O(n²) but fine for a single query.
            loop {
                let mut found_parent = false;
                for (pid, pinfo) in hierarchical.iter() {
                    for (cid, _) in hierarchical.children(pid) {
                        if cid == current_id {
                            segments.push(pinfo.name.clone().into_owned());
                            current_id = pid;
                            found_parent = true;
                            break;
                        }
                    }
                    if found_parent {
                        break;
                    }
                }
                if !found_parent {
                    break;
                }
            }
            break;
        }
    }

    segments.reverse();
    let qualified = segments.join("::");
    format!("{}::{}", file_path, qualified)
}
