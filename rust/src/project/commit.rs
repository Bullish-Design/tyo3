//! The single native commit funnel: classify, identity reconcile, staged transaction (Phase 5) + in-commit code-layer producer driver (Phase 6).
//!
//! Split from `project.rs` (Phase 13). Pulls shared imports + state
//! types from the parent module via `use super::*`.

use super::*;

// ── Sync-path resolver & event synthesis (Phase 3) ──────────────────────

/// Resolve a caller-supplied path to the absolute `SystemPathBuf` used as BOTH
/// the overlay store key AND the `ChangeEvent` path. Absolute → as-is; relative →
/// joined onto the project root. The leaf is never canonicalised (so deletes and
/// new paths are representable). The SAME value must key the store and the event,
/// or the overlay lookup inside `apply_changes` won't align.
pub(crate) fn resolve_sync_path(root: &SystemPath, path: &str) -> SystemPathBuf {
    let p = SystemPath::new(path);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        root.join(p)
    }
}

/// Classify a *content overlay* edit of a system path into a `ChangeEvent`.
/// New (not yet interned, or interned-but-absent) → `Created{File}`; otherwise an
/// existing file's content changed → `Changed{FileContent}`. Mirrors ty_server's
/// `open_document_in_db` "is_maybe_new_system_file" test.
///
/// Also checks disk via `ExistingPathKind::from_system` as a fallback for files
/// that exist on disk but have not yet been interned in salsa (lazy interning).
pub(crate) fn classify_overlay_edit(
    system: &OverlaySystem,
    db: &ProjectDatabase,
    path: &SystemPathBuf,
) -> ChangeEvent {
    let is_new = match db.files().try_system(db, path) {
        Some(f) => !f.exists(db),
        None => {
            // Not interned yet — check if the file exists on disk.
            // If it does, it's a change; if not, it's a creation.
            !matches!(
                ExistingPathKind::from_system(system, path),
                ExistingPathKind::File
            )
        }
    };
    if is_new {
        ChangeEvent::Created {
            path: path.clone(),
            kind: CreatedKind::File,
        }
    } else {
        ChangeEvent::file_content_changed(path.clone())
    }
}

/// Classify a *disk ingest* for `path`: first the overlay for this path has
/// been forgotten, so `ExistingPathKind::from_system` reads the underlying disk
/// truth through the overlay. Returns the appropriate `ChangeEvent`.
///
/// Phase 5 inlines the equivalent classification into `build_plan`'s `SyncPath`
/// arm (from the disk-read result + db interning, before the overlay is
/// republished), so this standalone helper is now exercised only by the
/// `sync_path_reingests_disk` unit test that documents the classification.
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) fn classify_disk_sync(
    system: &OverlaySystem,
    db: &ProjectDatabase,
    path: &SystemPathBuf,
) -> ChangeEvent {
    match ExistingPathKind::from_system(system, path) {
        ExistingPathKind::File => {
            let is_new = db.files().try_system(db, path).is_none_or(|f: File| !f.exists(db));
            if is_new {
                ChangeEvent::Created {
                    path: path.clone(),
                    kind: CreatedKind::File,
                }
            } else {
                ChangeEvent::Changed {
                    path: path.clone(),
                    kind: ChangedKind::Any,
                }
            }
        }
        // Directory or absent → treat as a (possibly recursive) delete.
        _ => ChangeEvent::Deleted {
            path: path.clone(),
            kind: DeletedKind::Any,
        },
    }
}

pub(crate) fn identity_scope_from_events(events: &[ChangeEvent]) -> Option<HashSet<String>> {
    let scope: HashSet<String> = events
        .iter()
        .filter_map(|event| event.system_path())
        .filter(|path| {
            path.extension()
                .and_then(PySourceType::try_from_extension)
                .is_some()
        })
        .map(|path| path.as_str().to_string())
        .collect();

    if scope.is_empty() {
        None
    } else {
        Some(scope)
    }
}

#[derive(Default)]
pub(crate) struct IdentityDelta {
    /// Minted ids this revision.
    created_ids: Vec<String>,
    /// Ids whose content hash changed (no over-fire).
    changed_ids: Vec<String>,
    /// Retired ids this revision (== `orphaned`).
    deleted_ids: Vec<String>,
    /// Structured moves: id + old/new qualified path + old/new file.
    moved: Vec<dto::MovedEntityDto>,
    /// Closure of `changed ∪ deleted` under the head code layer's reverse-deps.
    /// Phase 3: the layer is empty, so this equals the seeds.
    affected_ids: Vec<String>,
    /// Project-relative files of `affected_ids` (the files of the closure's
    /// nodes, `<external>` filtered) — emitted so the bus matches a
    /// reverse-dependent file-interest natively (§5.11).
    affected_files: Vec<String>,
    needs_review: Vec<String>,
    orphaned: Vec<String>,
    extracted: usize,
    scope_files: usize,
    /// DurableIds flagged `needs_review` that also have an authored record
    /// in a `review_on_change = true` layer.
    authored_needs_review: Vec<String>,
    /// DurableIds flagged `orphaned` that also have an authored record
    /// in a `review_on_change = true` layer.
    authored_orphaned: Vec<String>,
}

/// Assemble the public `CommitDeltaDto` from the id-level identity classes and
/// the path-shaped metadata the write method produced.
///
/// The nested `code_delta` (§6.2) is the minimal incremental delta from the
/// in-commit producer (`produce_layer` → `CodeLayer::diff_from`): `Some({…})`
/// for a structural change, `Some({})` for a cosmetic edit (applier no-op), and
/// a full rescan-flagged delta on `rescan` / cold start. `None` is only carried
/// by writes that don't reconcile (e.g. `author`). `touched_files` is the union
/// of the path-level created/changed/deleted strings — metadata only.
// Assembles the commit delta from distinct, independently-sourced components; a
// params struct would duplicate `CommitDeltaDto`'s own shape.
#[allow(clippy::too_many_arguments)]
pub(crate) fn build_commit_delta(
    revision: u64,
    root: &SystemPathBuf,
    created: Vec<String>,
    changed: Vec<String>,
    deleted: Vec<String>,
    identity: IdentityDelta,
    code_delta: Option<dto::CodeDeltaDto>,
    rescan: bool,
    project_changed: bool,
    custom_stdlib_changed: bool,
) -> dto::CommitDeltaDto {
    // `touched_files` is the bus's project-relative file surface (the directly
    // edited files); normalise the absolute native paths here so the bus is a
    // pure projection with no Python path math. The per-category `created` /
    // `changed` / `deleted` fields stay native (path-shaped metadata for
    // file-interest bus matching and human readability — never an entity id).
    let touched_files: Vec<String> = created
        .iter()
        .chain(changed.iter())
        .chain(deleted.iter())
        .map(|p| native_to_graph(root, p).unwrap_or_else(|| p.clone()))
        .collect();

    dto::CommitDeltaDto {
        revision,
        created_ids: identity.created_ids,
        changed_ids: identity.changed_ids,
        deleted_ids: identity.deleted_ids,
        moved: identity.moved,
        authored_ids: vec![],
        affected_ids: identity.affected_ids,
        code_delta,
        touched_files,
        affected_files: identity.affected_files,
        created,
        changed,
        deleted,
        needs_review: identity.needs_review,
        orphaned: identity.orphaned,
        authored_needs_review: identity.authored_needs_review,
        authored_orphaned: identity.authored_orphaned,
        identity_extracted: identity.extracted,
        identity_scope_files: identity.scope_files,
        rescan,
        project_changed,
        custom_stdlib_changed,
    }
}

/// Intersect the reconciliation `needs_review`/`orphaned` ids with the set
/// of ids that have an authored record in a `review_on_change = true` layer.
///
/// A layer with `review_on_change = false` contributes nothing here — its
/// records always report status `present` regardless of registry status.
pub(crate) fn compute_authored_lifecycle(
    config: &ValidatedConfig,
    authored: &AuthoredStore,
    nr_ids: &[String],
    orph_ids: &[String],
) -> (Vec<String>, Vec<String>) {
    // Collect the set of all durable ids that have an authored record in a
    // review_on_change layer.
    let mut monitored: std::collections::HashSet<String> = std::collections::HashSet::new();
    for name in &config.topo_order {
        let Some(layer_cfg) = config.raw.layers.get(name) else {
            continue;
        };
        if !matches!(layer_cfg.origin, config::LayerOrigin::Authored) {
            continue;
        }
        if !layer_cfg.review_on_change {
            continue;
        }
        for id in authored.ids_in_layer(name) {
            monitored.insert(id.to_string());
        }
    }

    let authored_nr: Vec<String> = nr_ids
        .iter()
        .filter(|id| monitored.contains(id.as_str()))
        .cloned()
        .collect();
    let authored_orph: Vec<String> = orph_ids
        .iter()
        .filter(|id| monitored.contains(id.as_str()))
        .cloned()
        .collect();
    (authored_nr, authored_orph)
}

/// Derive the authored record status for `(layer, id)` — a durable **level**
/// comparison against the body as it was when the note was authored.
///
/// Status is derived, not stored. Review-state is decoupled from the transient
/// registry `NeedsReview` status (an *edge* signal that auto-clears on the next
/// reconcile); it is the comparison `reviewed_hash != current_anchor_hash`.
/// Orphaned remains registry-driven (retire logic) and takes precedence.
///
/// ```text
/// status(layer, id) =
///     absent        — caller's job (no authored record)
///     present       if layer.review_on_change == false
///     orphaned      if registry anchor status == Orphaned
///     needs_review  if reviewed_hash.is_some() && reviewed_hash != current_anchor_hash
///     present       otherwise (incl. reviewed_hash == None — a v1/legacy record)
/// ```
pub(crate) fn derive_authored_status(
    config: &ValidatedConfig,
    registry: Option<&IdentityRegistry>,
    layer: &str,
    id: &str,
    reviewed_hash: Option<ContentHash>,
) -> String {
    // If review_on_change is false, always present.
    let review_on_change = config
        .raw
        .layers
        .get(layer)
        .map(|lc| lc.review_on_change)
        .unwrap_or(false);
    if !review_on_change {
        return "present".to_string();
    }

    let durable = DurableId(id.to_string());
    // Orphaned stays registry-driven and takes precedence over needs_review.
    if matches!(
        registry.and_then(|r| r.status_of(&durable)),
        Some(IdentityStatus::Orphaned)
    ) {
        return "orphaned".to_string();
    }
    // Level comparison: stale iff the body differs from the author-time hash.
    let current_hash = registry.and_then(|r| r.get(&durable)).map(|a| a.content_hash);
    match (reviewed_hash, current_hash) {
        (Some(rh), Some(ch)) if rh != ch => "needs_review".to_string(),
        _ => "present".to_string(),
    }
}

/// The durable **level** `needs_review` set: every durable id whose committed
/// body now differs from the hash captured when its note was authored, across
/// all `review_on_change = true` authored layers.
///
/// This is the trustworthy state the editor's layer diagnostic, `authored()
/// .status`, and the daemon `review_state` read — it survives saves, same-file
/// edits, restart, and unflags correctly on revert. Distinct from
/// `CommitDelta.needs_review`, the per-commit *edge* signal (ids that changed
/// in *this* commit and carry a note), which stays for the bus notification.
///
/// Orphaned anchors are excluded (orphaned is surfaced separately and takes
/// precedence); records with `reviewed_hash == None` (v1/legacy) are not
/// flagged until re-authored. Returns sorted, de-duplicated ids.
pub(crate) fn needs_review_ids(
    config: &ValidatedConfig,
    authored: &AuthoredStore,
    registry: &IdentityRegistry,
) -> Vec<String> {
    let mut flagged: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for name in &config.topo_order {
        let Some(layer_cfg) = config.raw.layers.get(name) else {
            continue;
        };
        if !matches!(layer_cfg.origin, config::LayerOrigin::Authored) {
            continue;
        }
        if !layer_cfg.review_on_change {
            continue;
        }
        for id in authored.ids_in_layer(name) {
            let Some(rec) = authored.records_get(name, id) else {
                continue;
            };
            let Some(reviewed) = rec.current.reviewed_hash else {
                continue;
            };
            let durable = DurableId(id.to_string());
            let Some(anchor) = registry.get(&durable) else {
                continue;
            };
            // Orphaned takes precedence; not a needs_review.
            if matches!(anchor.status, IdentityStatus::Orphaned) {
                continue;
            }
            if reviewed != anchor.content_hash {
                flagged.insert(id.to_string());
            }
        }
    }
    flagged.into_iter().collect()
}

/// Shared identity reconciliation: extract entities, reconcile against
/// registry, persist, and return identity delta fields.
///
/// `scope` is derived from the committed `ChangeEvent` paths. Identity anchors
/// are file-local (`qualified_path` begins with the defining file), so this is
/// the bounded closure needed by the identity layer; cross-file graph
/// invalidation continues to use the code graph's reverse-dependency index.
pub(crate) fn run_identity_reconciliation(
    head: &mut HeadState,
    scope: Option<&HashSet<String>>,
    next_rev: Revision,
) -> IdentityDelta {
    let state = TyProjectState {
        db: head.db.clone(),
        root: head.root.clone(),
        registry: None,
        hash_policy: head.hash_policy,
        hash_policies: head.hash_policies.clone(),
        default_hash_profile: head.default_hash_profile.clone(),
        authored: None,
    };
    let entities = match scope {
        Some(scope) => extract_entities_for(&state, scope),
        None => extract_entities(&state),
    };
    let extracted = entities.len();
    // Reconcile against the revision this staged commit WILL publish
    // (`next_rev`), not the store's current (pre-publish) revision — the store
    // bump is deferred to the publish tail (§5.3 publish-last).
    let recon = match scope {
        Some(scope) => reconcile_scoped(&mut head.registry, &entities, next_rev, scope),
        None => reconcile(&mut head.registry, &entities, next_rev),
    };

    // Id-level classification (§5.4): created / changed (hash-based, no over-fire)
    // / deleted / structured moves. Reads the old hashes captured on the bindings
    // before the in-pass rebind overwrote them.
    let classes = recon.classify(&entities);
    let created_ids: Vec<String> = classes.created.iter().map(|id| id.0.clone()).collect();
    let changed_ids: Vec<String> = classes.changed.iter().map(|id| id.0.clone()).collect();
    let deleted_ids: Vec<String> = classes.deleted.iter().map(|id| id.0.clone()).collect();
    let moved: Vec<dto::MovedEntityDto> = classes
        .moved
        .iter()
        .map(|m| dto::MovedEntityDto {
            id: m.id.0.clone(),
            old_qualified_path: m.old_qualified_path.clone(),
            new_qualified_path: m.new_qualified_path.clone(),
            old_file: m.old_file.clone(),
            new_file: m.new_file.clone(),
        })
        .collect();

    let needs_review: Vec<String> = recon.needs_review.iter().map(|id| id.0.clone()).collect();
    let orphaned: Vec<String> = recon.retired.iter().map(|id| id.0.clone()).collect();

    // affected_ids is NOT computed here: the in-commit producer (§6.1) runs
    // AFTER reconciliation and maintains `head.code_layer.reverse_deps`, so the
    // transitive, container-granular closure is computed in `run_staged` over
    // the freshly-updated layer (seeding deletions from the prior layer, §6.3).
    // Left empty here and overwritten there.
    let affected_ids: Vec<String> = Vec::new();

    // Authored lifecycle surfacing: intersect reconciliation output with
    // authored record ids in `review_on_change = true` layers.
    let (authored_needs_review, authored_orphaned) =
        compute_authored_lifecycle(&head.config, &head.authored, &needs_review, &orphaned);

    // NOTE (Phase 5): identity persistence is NO LONGER done here. It used to
    // `write_atomic` the registry and *swallow* the error (log-and-continue),
    // which is the §5.3 "succeeds in memory but fails to persist" / §5.12 "do
    // not swallow" violation this phase removes. Persistence is now a staged,
    // fallible, *propagated* step in `commit` (`persist_identity` → the typed
    // `SidecarWriteError`), run BEFORE the deferred publish so a write failure
    // rolls the whole commit back to R−1.

    // NOTE: the code layer is *not* produced in this reconciliation pass. A full
    // build (occurrence resolution + type-hierarchy/typeshed warmup) is ~100x too
    // slow to run on the GIL per commit, so `run_staged` runs the *scoped*
    // producer (`produce_layer`) over just the dirty scope and maintains
    // `reverse_deps` incrementally; `PyTyProject::full_code_delta` serves a full
    // build on demand. This keeps identity reconciliation cheap and bounded.

    IdentityDelta {
        created_ids,
        changed_ids,
        deleted_ids,
        moved,
        affected_ids,
        // Computed in `run_staged` over the maintained `next` layer (the node→file
        // map of the closure), after `affected_ids` is resolved.
        affected_files: Vec::new(),
        needs_review,
        orphaned,
        extracted,
        scope_files: scope.map_or(0, |scope| scope.len()),
        authored_needs_review,
        authored_orphaned,
    }
}

// ── Phase 5: the single native commit(mutation) funnel ───────────────────
//
// Every write kind (edit / edit_many / edit_virtual / sync_path / discard /
// sync_all / author / watcher poll_changes) funnels through `commit`. It STAGES
// all next-state, runs every fallible step (serialise, write_atomic, engine
// apply, code-layer stage, delta compute) against the stage, and PUBLISHES the
// revision LAST (`ContentStore::publish_staged`). A failure in any in-lock step
// returns a typed error and rolls back to R−1 with no torn publish and no bus
// delta (§5.3). Strategy B+ ("deferred publish"): the content store — the
// observable `head` revision, snapshot content, and retained window — is never
// mutated until the publish tail, so a failed commit cannot have advanced it.
// The registry/authored/overlay are mutated in place (reconciliation must run
// the live db over the staged content) and restored from a baseline on failure;
// the salsa db is left benignly ahead (it re-reads the restored overlay → same
// content → recomputes equal) because salsa cannot be rolled back: it shares
// `Zalsa` storage with any clone, and a rollback clone would deadlock the next
// `apply_changes` (it blocks until it is the sole live handle).

/// What a write does, decoupled from how it commits.
pub(crate) enum Mutation {
    /// edit / edit_many / edit_virtual — overlay buffer edits already classified
    /// against the pre-edit content by the thin PyO3 method.
    Overlay {
        changes: Vec<crate::content::Change>,
        events: Vec<ChangeEvent>,
        created: Vec<String>,
        changed: Vec<String>,
        /// System paths that gained a genuinely unsaved buffer (for the watcher
        /// buffer-wins set). Empty for virtual edits.
        unsaved: Vec<SystemPathBuf>,
    },
    /// sync_path / discard — re-read one path from disk.
    SyncPath { abs: SystemPathBuf },
    /// sync_all — re-ingest the whole project from disk (one rescan revision).
    SyncAll,
    /// author — write one authored record (no content / engine change).
    Author { layer: String, id: String, value: serde_json::Value },
    /// watcher poll_changes — fold a drained batch of disk events.
    Poll { events: Vec<ChangeEvent> },
}

/// An authored record staged for persistence (the §5.3 stage → persist →
/// publish ordering). Built before any state moves; persisted as a staged step;
/// the in-memory store swap happens only at the publish tail.
pub(crate) struct AuthoredPlan {
    next_store: AuthoredStore,
    record_path: std::path::PathBuf,
    bytes: Vec<u8>,
    id: String,
}

/// The fully-built next-state of a commit, ready for the staged transaction.
/// Nothing here is observable to a reader until `run_staged` reaches its tail.
pub(crate) struct StagedCommit {
    /// The next generation, built without advancing the store.
    staged_gen: Generation,
    /// Engine change events to apply to the head db.
    events: Vec<ChangeEvent>,
    /// Path-shaped delta metadata (Phase 3 still carries these alongside ids).
    created: Vec<String>,
    changed: Vec<String>,
    deleted: Vec<String>,
    rescan: bool,
    /// Identity reconciliation scope (None ⇒ full reconcile).
    scope: Option<HashSet<String>>,
    /// Run identity reconciliation + persist the registry? (false for author.)
    reconcile: bool,
    /// Authored record to persist + swap in at the publish tail (author only).
    authored: Option<AuthoredPlan>,
    /// Paths to add to the unsaved-overlay set at the publish tail.
    unsaved_insert: Vec<SystemPathBuf>,
    /// Paths to drop from the unsaved-overlay set at the publish tail.
    unsaved_remove: Vec<SystemPathBuf>,
}

/// State a failed commit must restore. The store is NOT here: it is never
/// mutated before the publish tail (deferred publish), so there is nothing to
/// undo for it.
pub(crate) struct Baseline {
    published_gen: Generation,
    registry: IdentityRegistry,
    authored: AuthoredStore,
    /// The code layer before the in-commit producer ran. A failed commit must
    /// restore it so a torn (half-re-derived) layer is never observable (§6.1 /
    /// rollback test 3).
    code_layer: crate::code_layer::CodeLayer,
}

/// TEST-ONLY: fire the armed one-shot fault if it names `stage`. The arm is
/// taken once at the top of the commit, so this is a pure check; it returns the
/// typed error the rollback contract expects (a sidecar persist failure →
/// `SidecarWriteError`; a non-sidecar stage → `CommitFailed`). Inert (always
/// `Ok`) unless the test armed exactly this stage.
pub(crate) fn check_fault(armed: &Option<String>, stage: &str) -> PyResult<()> {
    if armed.as_deref() == Some(stage) {
        return Err(match stage {
            "identity_persist" | "authored_persist" => {
                SidecarWriteError::new_err(format!("injected commit fault at stage {stage:?}"))
            }
            other => CommitFailed::new_err(format!("injected commit fault at stage {other:?}")),
        });
    }
    Ok(())
}

/// Serialise the reconciled identity registry and persist it crash-safely.
/// Propagates a `SidecarWriteError` on a write failure (§5.12 — never swallowed,
/// as the pre-Phase-5 `log::error!`-and-continue did), so the `?` short-circuits
/// BEFORE the deferred publish and the commit rolls back.
pub(crate) fn persist_identity(head: &HeadState) -> PyResult<()> {
    let bytes = head.registry.to_bytes().map_err(|e| {
        CommitFailed::new_err(format!("failed to serialise identity registry: {e}"))
    })?;
    let identity_path = head.sidecar.identity_db_path();
    head.sidecar.write_atomic(&identity_path, &bytes).map_err(|e| {
        SidecarWriteError::new_err(format!(
            "failed to persist identity registry at {identity_path:?}: {e}"
        ))
    })
}

/// Restore the baseline after a failed commit and hand the error back. The store
/// is untouched (deferred publish), so head / retained / snapshot content are
/// already at R−1; this re-publishes the prior generation to the live overlay
/// and restores the in-place registry / authored mutations.
pub(crate) fn rollback(head: &mut HeadState, base: Baseline, err: PyErr) -> PyErr {
    head.system.publish(base.published_gen);
    head.registry = base.registry;
    head.authored = base.authored;
    head.code_layer = base.code_layer;
    err
}

/// A disk-read result → a store `Change` (Insert with content, or a tombstone
/// when the file could not be read).
pub(crate) fn disk_change(path: &SystemPathBuf, disk_text: std::io::Result<String>) -> crate::content::Change {
    match disk_text {
        Ok(text) => crate::content::Change::Insert { path: path.clone(), text: Arc::from(text) },
        Err(_) => crate::content::Change::Delete { path: path.clone() },
    }
}

/// Build the next-state plan for `mutation` WITHOUT mutating any observable head
/// state. Returns `Ok(None)` for a no-op (an empty watcher poll), or `Err` for a
/// pre-stage validation failure (author config / JSON / id checks). Disk reads,
/// event classification, and authored serialisation all happen here, before the
/// staged transaction begins.
pub(crate) fn build_plan(head: &mut HeadState, mutation: Mutation) -> PyResult<Option<StagedCommit>> {
    match mutation {
        Mutation::Overlay { changes, events, created, changed, unsaved } => {
            let staged_gen = head.store.stage(changes);
            let scope = identity_scope_from_events(&events);
            Ok(Some(StagedCommit {
                staged_gen,
                events,
                created,
                changed,
                deleted: vec![],
                rescan: false,
                scope,
                reconcile: true,
                authored: None,
                unsaved_insert: unsaved,
                unsaved_remove: vec![],
            }))
        }
        Mutation::SyncPath { abs } => {
            // Read disk once; classify Created/Changed/Deleted from the result
            // and the db's current interning (equivalent to classify_disk_sync,
            // but without depending on the overlay being already republished).
            let disk_text = std::fs::read_to_string(abs.as_std_path());
            let path_str = abs.as_str().to_string();
            let (change, event, created, changed, deleted) = match disk_text {
                Ok(text) => {
                    let is_new = head
                        .db
                        .files()
                        .try_system(&head.db, &abs)
                        .is_none_or(|f: File| !f.exists(&head.db));
                    let change = crate::content::Change::Insert {
                        path: abs.clone(),
                        text: Arc::from(text),
                    };
                    if is_new {
                        (change,
                         ChangeEvent::Created { path: abs.clone(), kind: CreatedKind::File },
                         vec![path_str], vec![], vec![])
                    } else {
                        (change,
                         ChangeEvent::Changed { path: abs.clone(), kind: ChangedKind::Any },
                         vec![], vec![path_str], vec![])
                    }
                }
                Err(_) => (
                    crate::content::Change::Delete { path: abs.clone() },
                    ChangeEvent::Deleted { path: abs.clone(), kind: DeletedKind::Any },
                    vec![], vec![], vec![path_str],
                ),
            };
            let staged_gen = head.store.stage(vec![change]);
            // Syncing a project-config file is a coarse change (§5.4): rescan.
            let rescan = crate::content::is_project_config_file(&abs);
            let scope = if rescan {
                None
            } else {
                identity_scope_from_events(std::slice::from_ref(&event))
            };
            Ok(Some(StagedCommit {
                staged_gen,
                events: vec![event],
                created,
                changed,
                deleted,
                rescan,
                scope,
                reconcile: true,
                authored: None,
                unsaved_insert: vec![],
                // A disk sync supersedes any unsaved buffer for this path.
                unsaved_remove: vec![abs],
            }))
        }
        Mutation::SyncAll => {
            // Re-ingest the whole project from disk so files created/changed
            // after open are discovered (Phase 5 carry-over of the Phase 1
            // `sync_all` gap). Unsaved overlay buffers are preserved.
            let root = head.root.clone();
            let skip = head.unsaved_overlays.clone();
            let staged_gen = head.store.stage_reingest(&root, is_project_relevant, &skip);
            Ok(Some(StagedCommit {
                staged_gen,
                events: vec![ChangeEvent::Rescan],
                created: vec![],
                changed: vec![],
                deleted: vec![],
                rescan: true,
                scope: None,
                reconcile: true,
                authored: None,
                unsaved_insert: vec![],
                unsaved_remove: vec![],
            }))
        }
        Mutation::Author { layer, id, value } => {
            // Pre-stage validation (may return ConfigError / ValueError before
            // any state moves).
            let lcfg = head.config.authored_layer_config(&layer).ok_or_else(|| {
                PyConfigError::new_err(format!("'{layer}' is not a declared authored layer"))
            })?;
            let history = lcfg.history;
            let durable_id = DurableId(id.clone());
            if head.registry.get(&durable_id).is_none() {
                return Err(PyValueError::new_err(format!(
                    "durable id '{id}' is not known to the identity registry"
                )));
            }
            // Stamp the entity's *current* committed body hash from the registry
            // anchor (never recompute it — it must match `Anchor.content_hash`
            // exactly). This is the durable review-state baseline: a later body
            // change makes `reviewed_hash != current_anchor_hash` → needs_review;
            // re-authoring re-stamps `= current`, which is the acknowledge path.
            let reviewed_hash = head.registry.get(&durable_id).map(|a| a.content_hash);
            // Stage the copy-on-write authored store and serialise the record —
            // do NOT swap it into head yet (that is the publish tail, §5.3).
            let version = crate::authored::AuthoredVersion {
                value,
                revision: head.store.next_revision().0,
                reviewed_hash,
            };
            let new_map = head.authored.put(&layer, &id, version, history);
            let next_store = AuthoredStore::new(new_map);
            let rec = next_store.records_get(&layer, &id).unwrap();
            let doc = AuthoredRecordDoc {
                format_version: crate::authored::AUTHORED_FORMAT_VERSION,
                layer: layer.clone(),
                durable_id: id.clone(),
                current: rec.current.clone(),
                history: rec.history.clone(),
            };
            let bytes = doc.to_bytes().map_err(|e| {
                CommitFailed::new_err(format!("failed to serialise authored record: {e}"))
            })?;
            let record_path = head.sidecar.record_path(&layer, &id);
            Ok(Some(StagedCommit {
                // No content change: the authored edit is a revision with
                // identical content (an empty bump, retained).
                staged_gen: head.store.stage_unchanged(),
                events: vec![],
                created: vec![],
                changed: vec![],
                deleted: vec![],
                rescan: false,
                scope: None,
                reconcile: false,
                authored: Some(AuthoredPlan { next_store, record_path, bytes, id }),
                unsaved_insert: vec![],
                unsaved_remove: vec![],
            }))
        }
        Mutation::Poll { events } => build_poll_plan(head, events),
    }
}

/// Build the staged plan for a drained watcher batch (the old `apply_watch_events`
/// fold). Returns `Ok(None)` when nothing survives filtering, so `poll_changes`
/// returns `None` WITHOUT bumping the revision (a no-op, never a published empty
/// revision).
pub(crate) fn build_poll_plan(
    head: &mut HeadState,
    events: Vec<ChangeEvent>,
) -> PyResult<Option<StagedCommit>> {
    if events.is_empty() {
        return Ok(None);
    }
    // Rescan short-circuit: if ty lost sync, redo everything (like sync_all, but
    // the watcher path stages the current content — the engine re-walks disk).
    if events.iter().any(|e| e.is_rescan()) {
        return Ok(Some(StagedCommit {
            staged_gen: head.store.stage_unchanged(),
            events: vec![ChangeEvent::Rescan],
            created: vec![],
            changed: vec![],
            deleted: vec![],
            rescan: true,
            scope: None,
            reconcile: true,
            authored: None,
            unsaved_insert: vec![],
            unsaved_remove: vec![],
        }));
    }

    // Keep only real-path events whose path is NOT a genuinely unsaved buffer
    // (the buffer-wins rule, now gated on `unsaved_overlays`, not `has_overlay`
    // — Phase 1 interns every file at open, so `has_overlay` would drop them all).
    let mut store_changes: Vec<crate::content::Change> = Vec::with_capacity(events.len());
    let mut kept_events: Vec<ChangeEvent> = Vec::with_capacity(events.len());
    let (mut created, mut changed, mut deleted) = (Vec::new(), Vec::new(), Vec::new());
    for event in events {
        let Some(path) = event.system_path() else {
            continue;
        };
        let path = path.to_path_buf();
        if head.unsaved_overlays.contains(&path) {
            // Unsaved buffer wins; ignore the disk event.
            continue;
        }
        let path_str = path.as_str().to_string();
        let disk_text = std::fs::read_to_string(path.as_std_path());
        match &event {
            ChangeEvent::Created { .. } => {
                created.push(path_str);
                store_changes.push(disk_change(&path, disk_text));
            }
            ChangeEvent::Deleted { .. } => {
                deleted.push(path_str);
                store_changes.push(crate::content::Change::Delete { path: path.clone() });
            }
            ChangeEvent::Changed { .. } => {
                changed.push(path_str);
                store_changes.push(disk_change(&path, disk_text));
            }
            _ => continue,
        }
        kept_events.push(event);
    }

    if kept_events.is_empty() {
        return Ok(None);
    }

    let staged_gen = head.store.stage(store_changes);
    let scope = identity_scope_from_events(&kept_events);
    Ok(Some(StagedCommit {
        staged_gen,
        events: kept_events,
        created,
        changed,
        deleted,
        rescan: false,
        scope,
        reconcile: true,
        authored: None,
        unsaved_insert: vec![],
        unsaved_remove: vec![],
    }))
}

/// Convert an absolute native path string to a project-relative POSIX graph
/// path (matching `code_layer::Builder::to_graph_path`). Returns `None` for a
/// path outside the root (external — no graph node).
pub(crate) fn native_to_graph(root: &SystemPathBuf, native: &str) -> Option<String> {
    let rest = native.strip_prefix(root.as_str())?;
    let rest = rest.strip_prefix('/').unwrap_or(rest);
    (!rest.is_empty()).then(|| rest.to_string())
}

/// Drive the in-commit code-layer producer (§6.1/6.2). Returns the next layer
/// and the minimal incremental `code_delta`.
///
/// - `rescan` (or an unscoped write) → a full build diffed against an empty
///   layer, i.e. a full `rescan`-flagged delta the Python applier applies
///   wholesale.
/// - an empty `prev` (first write of a session — the head layer is built lazily,
///   never at open, to keep `open()` off the producer's cost path) → a one-time
///   full build, likewise emitted as a full delta.
/// - otherwise → the scoped producer over the dirty graph paths (the identity
///   scope = changed ∪ created ∪ deleted), expanding one-hop importers and
///   maintaining `reverse_deps` edge-by-edge.
pub(crate) fn produce_layer(
    state: &TyProjectState,
    prev: &crate::code_layer::CodeLayer,
    staged: &StagedCommit,
    next_rev: Revision,
) -> (crate::code_layer::CodeLayer, dto::CodeDeltaDto) {
    use crate::code_layer::{produce_code_delta, produce_code_delta_scoped, CodeLayer};
    let rev = next_rev.0;
    let dirty: Option<HashSet<String>> = staged.scope.as_ref().map(|s| {
        s.iter()
            .filter_map(|n| native_to_graph(&state.root, n))
            .collect()
    });
    match dirty {
        Some(dirty) if !staged.rescan && !prev.is_empty() => {
            produce_code_delta_scoped(state, prev, &dirty, rev)
        }
        // Full build: rescan, cold start (empty prev), or an unscoped write.
        _ => produce_code_delta(state, &CodeLayer::new(), rev, staged.rescan, None),
    }
}

/// Run the staged transaction: publish the staged content to the live overlay,
/// apply the engine change, reconcile, fire the staged fault boundaries, persist
/// the sidecar, and PUBLISH the revision LAST. Any `?` failure leaves the store
/// untouched (the publish tail was never reached) and propagates so the caller
/// rolls the in-place mutations back.
pub(crate) fn run_staged(
    head: &mut HeadState,
    staged: StagedCommit,
    armed: &Option<String>,
) -> PyResult<dto::CommitDeltaDto> {
    let next_rev = head.store.next_revision();

    // 1. Publish the staged content to the live overlay so the head db analyses
    //    the next generation. (Observable only to lock-guarded floating reads;
    //    new snapshots pin the store, which is still at R−1.)
    head.system.publish(Arc::clone(&staged.staged_gen));

    // 2. Apply the engine change (skipped for authored writes — no content
    //    changed, so the db must not be touched).
    let (project_changed, custom_stdlib_changed) = if staged.events.is_empty() {
        (false, false)
    } else {
        let result = head.db.apply_changes(&staged.events, None);
        (result.project_changed(), result.custom_stdlib_changed())
    };

    // 3 + 4 + 5. Identity reconcile → code-layer produce → identity persist.
    let (identity, code_delta) = if staged.reconcile {
        let mut identity = run_identity_reconciliation(head, staged.scope.as_ref(), next_rev);

        // ── Scoped in-commit code-layer producer (§6.1/6.2/6.3) ──
        // Re-derive the dirty scope over the prior layer, update head.code_layer,
        // and emit the minimal incremental code_delta. Runs inside the lock,
        // before the deferred publish; rolled back via the captured Baseline.
        let prev = std::mem::take(&mut head.code_layer);
        let state = head.read_clone();
        let (next, code_delta) = produce_layer(&state, &prev, &staged, next_rev);

        // affected_ids = transitive, container-granular closure of changed ∪
        // deleted over the freshly-maintained reverse_deps, seeding deletions
        // from the prior layer (§6.3).
        let seeds: std::collections::BTreeSet<String> = identity
            .changed_ids
            .iter()
            .chain(identity.deleted_ids.iter())
            .cloned()
            .collect();
        let deleted: std::collections::BTreeSet<String> =
            identity.deleted_ids.iter().cloned().collect();
        identity.affected_ids = next
            .affected_closure_with_deleted(&prev, &seeds, &deleted)
            .into_iter()
            .collect();

        // affected_files = the project-relative files of the affected closure's
        // nodes (deduped, `<external>` filtered) — emitted natively so the bus
        // matches a reverse-dependent file-interest without walking a graph
        // (§5.11). Deleted seeds are absent from `next` and resolve via
        // `touched_files` (their deleted paths) instead, so missing them here is
        // correct, not a gap.
        let affected_files: std::collections::BTreeSet<String> = identity
            .affected_ids
            .iter()
            .filter_map(|id| next.nodes.get(id))
            .map(|node| node.file.clone())
            .filter(|file| file != "<external>")
            .collect();
        identity.affected_files = affected_files.into_iter().collect();

        head.code_layer = next;

        // Code-layer staging boundary fault seam: a producer/code-layer failure
        // must publish no partial revision (rollback test 3).
        check_fault(armed, "code_layer")?;
        // Identity persistence: a staged, fallible, PROPAGATED step.
        check_fault(armed, "identity_persist")?;
        persist_identity(head)?;
        (identity, Some(code_delta))
    } else {
        (IdentityDelta::default(), None)
    };

    // 6. Authored persistence (author only): stage → persist (publish at tail).
    if let Some(ap) = &staged.authored {
        check_fault(armed, "authored_persist")?;
        head.sidecar.write_atomic(&ap.record_path, &ap.bytes).map_err(|e| {
            SidecarWriteError::new_err(format!(
                "failed to persist authored record at {:?}: {e}",
                ap.record_path
            ))
        })?;
    }

    // 7. PUBLISH LAST — the single, last in-lock observable mutation. After this
    //    point nothing can fail; before it, every failure path left the store at
    //    R−1.
    let revision = head.store.publish_staged(staged.staged_gen).0;

    // Swap in the authored store and update the unsaved-overlay set now that the
    // commit is irrevocable.
    let authored_ids = if let Some(ap) = staged.authored {
        head.authored = ap.next_store;
        vec![ap.id]
    } else {
        vec![]
    };
    for p in staged.unsaved_insert {
        head.unsaved_overlays.insert(p);
    }
    for p in &staged.unsaved_remove {
        head.unsaved_overlays.remove(p);
    }

    // 8. Build the id-level commit delta.
    let mut dto = build_commit_delta(
        revision,
        &head.root,
        staged.created,
        staged.changed,
        staged.deleted,
        identity,
        code_delta,
        staged.rescan,
        project_changed,
        custom_stdlib_changed,
    );
    if !authored_ids.is_empty() {
        dto.authored_ids = authored_ids;
    }
    Ok(dto)
}

/// The single native commit funnel (§5.3). Builds the plan, captures the
/// rollback baseline, takes the one-shot fault arm, runs the staged transaction,
/// and rolls back on any failure. Returns `Ok(None)` for a no-op (an empty
/// watcher poll → Python `None`, NOT a published empty revision).
pub(crate) fn commit(head: &mut HeadState, mutation: Mutation) -> PyResult<Option<dto::CommitDeltaDto>> {
    let Some(staged) = build_plan(head, mutation)? else {
        return Ok(None);
    };
    let base = Baseline {
        published_gen: head.store.capture(),
        registry: head.registry.clone(),
        authored: head.authored.clone(),
        code_layer: head.code_layer.clone(),
    };
    // One-shot: consumed by THIS commit whether or not its stage is reached, so
    // a stale arm can never leak into a later unrelated write.
    let armed = head.armed_fault.take();
    match run_staged(head, staged, &armed) {
        Ok(dto) => Ok(Some(dto)),
        Err(e) => Err(rollback(head, base, e)),
    }
}

/// Pythonize a commit result for the write methods that always publish a
/// revision (every kind except `poll_changes`). A `None` here would mean a
/// staged plan reported a no-op for a write that must produce a delta — a bug,
/// surfaced as `CommitFailed` rather than silently swallowed.
pub(crate) fn commit_dto_to_py<'py>(
    py: Python<'py>,
    dto: Option<dto::CommitDeltaDto>,
) -> PyResult<Bound<'py, PyAny>> {
    let dto = dto.ok_or_else(|| CommitFailed::new_err("commit produced no delta"))?;
    pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

