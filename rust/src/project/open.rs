//! Project open / HEAD construction (authored load, build_head, build_frozen).
//!
//! Split from `project.rs` (Phase 13). Pulls shared imports + state
//! types from the parent module via `use super::*`.

use super::*;

// ── Authored records ──────────────────────────────────────────────────────

/// Load all authored records from disk for every layer declared with
/// `origin = "authored"`.  Returns an `AuthoredStore` (rpds-backed map)
/// containing every valid record on disk.  A missing `authored/` directory
/// yields an empty store (first run).  Corrupt or newer-format records
/// propagate `AuthoredLoadError` upward so the session does not open
/// (§11.3.5).
pub(crate) fn load_authored_records(
    config: &ValidatedConfig,
    sidecar: &Sidecar,
) -> Result<AuthoredStore, AuthoredLoadError> {
    let mut map = AuthoredMap::default();
    for name in &config.topo_order {
        let Some(layer_cfg) = config.raw.layers.get(name) else {
            continue;
        };
        if !matches!(layer_cfg.origin, config::LayerOrigin::Authored) {
            continue;
        }
        let dir = sidecar.authored_dir(name);
        if !dir.exists() {
            continue;
        }
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            // Skip dirs, non-.json files, and temp files.
            if !path.is_file() {
                continue;
            }
            let Some(_file_name) = path.file_stem().and_then(|n| n.to_str()) else {
                continue;
            };
            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let bytes = match std::fs::read(&path) {
                Ok(b) => b,
                Err(_) => continue,
            };
            let doc = AuthoredRecordDoc::from_bytes(&bytes)?;
            // History from disk: re-insert each historical version if the
            // layer keeps history.  Put history versions in order (oldest
            // first), then current — each `put` pushes the previous current
            // into history, so the final record has the full history chain.
            if layer_cfg.history {
                for hist_version in &doc.history {
                    map = map.put(
                        &doc.layer,
                        &doc.durable_id,
                        hist_version.clone(),
                        layer_cfg.history,
                    );
                }
            }
            map = map.put(
                &doc.layer,
                &doc.durable_id,
                doc.current,
                layer_cfg.history,
            );
        }
    }
    Ok(AuthoredStore::new(map))
}

// ── Head builder ──────────────────────────────────────────────────────────

/// Build a live HEAD with the default config. Test-only convenience.
#[cfg(test)]
pub(crate) fn build_head(root: SystemPathBuf, initial_store: ContentStore, registry: IdentityRegistry) -> HeadState {
    let config = config::validate(RawConfig::defaults()).expect("default config is valid");
    build_head_with_config(root, initial_store, registry, config)
}

/// Build a live HEAD over an `OverlaySystem`, seeding the overlay from
/// `initial_store` (an empty store on first open; the preserved store on reload).
///
/// Construction mirrors ty_server (`ty_server/src/session.rs:602`), but keeps
/// the caller's root as a hard project boundary. `ProjectMetadata::discover`
/// walks upward, which is correct for a CLI but wrong for TyO3 fixture/session
/// roots: opening `fixtures/simple_package` must not analyze the whole repo.
///
/// Steps:
///   1. discover project metadata from disk (`pyproject.toml` / `ty.toml`),
///   2. discard discovered metadata if it came from an ancestor root,
///   3. layer user-level configuration on top,
///   4. build the db with `fallible` (surfaces config errors),
///   5. on any failure, fall back to a default blank project (never panic).
pub(crate) fn build_head_with_config(
    root: SystemPathBuf,
    initial_store: ContentStore,
    registry: IdentityRegistry,
    config: ValidatedConfig,
) -> HeadState {
    use ruff_python_ast::name::Name;

    // The overlay the db reads through. The clone handed to fallible/use_defaults
    // shares the same content cell, so `system.publish(...)` (Phase 3) is visible
    // to the db.
    let system = OverlaySystem::live(root.clone(), initial_store.capture());

    // 1+2+3+4: discover → clamp → apply user config → build. Each step's error
    // is mapped to a string so the chain has one error type (no `anyhow`
    // dependency).
    let built: Result<ProjectDatabase, String> = ProjectMetadata::discover(&root, &system)
        .map_err(|e| format!("project discovery failed: {e}"))
        .map(|metadata| {
            if metadata.root() == &*root {
                metadata
            } else {
                ProjectMetadata::new(
                    Name::new(root.file_name().unwrap_or("root")),
                    root.clone(),
                )
            }
        })
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
            log::warn!("{err}. Falling back to default project settings.");
            let metadata = ProjectMetadata::new(Name::new("tyo3-project"), root.clone());
            ProjectDatabase::use_defaults(metadata, system.clone())
        }
    };

    let hash_policies: HashMap<String, HashPolicy> = config
        .raw
        .hashing
        .profiles
        .iter()
        .map(|(name, profile)| (name.clone(), HashPolicy::from(profile)))
        .collect();
    let default_hash_profile = config.raw.spine.default_hash_profile.clone();

    HeadState {
        db,
        sidecar: Sidecar::new(root.as_std_path()),
        root,
        store: initial_store,
        system,
        registry,
        hash_policy: config.hash_policy_for("code"),
        hash_policies,
        default_hash_profile,
        config,
        authored: AuthoredStore::default(),
        code_layer: crate::code_layer::CodeLayer::new(),
        unsaved_overlays: HashSet::new(),
        armed_fault: None,
    }
}

/// Build an independent, revision-pinned `ProjectDatabase` over a frozen overlay.
///
/// Construction mirrors `build_head` (discover → apply user config → fallible,
/// with a `use_defaults` fallback) but over `OverlaySystem::frozen(...)`, so the
/// resulting db has its OWN `Zalsa`: it can never be cancelled by a HEAD
/// `apply_changes`, and a HEAD `apply_changes` never blocks on it (architecture §0).
///
/// `generation` is the content pinned at `rev` (captured from the store, O(1)).
/// Since Phase 1, the generation is already *complete* for revision R: every
/// project-relevant file (Python sources plus the `pyproject.toml` / `ty.toml`
/// config files that `ProjectMetadata::discover` / `apply_configuration_files`
/// read) was interned at open and at every commit. The frozen overlay therefore
/// pins exactly this generation with no disk fallback (overlay.rs §2), so the
/// snapshot never races live disk and capture stays O(1).
pub(crate) fn build_frozen(
    root: SystemPathBuf,
    generation: Generation,
    rev: Revision,
    hash_policies: HashMap<String, HashPolicy>,
    default_hash_profile: String,
) -> TyProjectState {
    // Phase 1: the generation is already complete (ingested at open and
    // updated at every commit), so no disk pre-population is needed.
    // The frozen overlay pins exactly the generation as-is, achieving
    // O(1) snapshot capture with no disk content reads.
    let system = OverlaySystem::frozen(root.clone(), generation, rev);

    let built: Result<ProjectDatabase, String> = ProjectMetadata::discover(&root, &system)
        .map_err(|e| format!("project discovery failed: {e}"))
        .map(|metadata| {
            if metadata.root() == &*root {
                metadata
            } else {
                ProjectMetadata::new(
                    Name::new(root.file_name().unwrap_or("root")),
                    root.clone(),
                )
            }
        })
        .and_then(|mut metadata| {
            metadata
                .apply_configuration_files(&system)
                .map_err(|e| format!("failed to apply configuration files: {e}"))?;
            ProjectDatabase::fallible(metadata, system.clone())
                .map_err(|e| format!("failed to build snapshot database: {e:#}"))
        });

    let db = match built {
        Ok(db) => db,
        Err(err) => {
            log::warn!("{err}. Falling back to default project settings for snapshot.");
            let metadata =
                ProjectMetadata::new(Name::new("tyo3-project"), root.clone());
            ProjectDatabase::use_defaults(metadata, system)
        }
    };

    TyProjectState {
        db,
        root,
        registry: None,
        hash_policy: if let Some(p) = hash_policies.get(&default_hash_profile) {
            *p
        } else {
            HashPolicy::default()
        },
        hash_policies,
        default_hash_profile,
        authored: None,
    }
}

