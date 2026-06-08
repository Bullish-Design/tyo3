//! Typed `.tyo3/config.toml` loading (SPEC §11.2).
//!
//! Gate 4 keeps parsing in Rust so later gates consume one defaulted,
//! normalised config tree. Validation is layered on top in Step 3.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;

use crate::hash::HashPolicy;
use crate::sidecar::Sidecar;

pub const CURRENT_SCHEMA_VERSION: u32 = 1;

#[derive(Debug)]
pub enum ConfigError {
    UnknownVersion(u32),
    DanglingRef { key: String, target: String },
    ReservedName(String),
    Cycle(String),
    OriginViolation(String),
    UnknownKind(String),
    DimMismatch(String),
    SecretInline(String),
    Parse(String),
    UndefinedEnv(String),
    BlockingOverflow(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::UnknownVersion(v) => write!(f, "unsupported schema_version: {v}"),
            ConfigError::DanglingRef { key, target } => {
                write!(f, "dangling config reference {key} -> {target}")
            }
            ConfigError::ReservedName(name) => write!(f, "reserved layer name: {name}"),
            ConfigError::Cycle(cycle) => write!(f, "layer dependency cycle: {cycle}"),
            ConfigError::OriginViolation(s) => write!(f, "layer origin violation: {s}"),
            ConfigError::UnknownKind(kind) => write!(f, "unknown entity kind: {kind}"),
            ConfigError::DimMismatch(s) => write!(f, "generator/store dimension mismatch: {s}"),
            ConfigError::SecretInline(key) => write!(f, "inline secret-like value at {key}"),
            ConfigError::Parse(s) => write!(f, "config parse error: {s}"),
            ConfigError::UndefinedEnv(name) => {
                write!(f, "undefined environment variable in config: {name}")
            }
            ConfigError::BlockingOverflow(policy) => write!(
                f,
                "writer-blocking or unknown bus overflow policy: {policy} \
                 (use one of: coalesce, drop_and_mark_lagged, error_and_close)"
            ),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RawConfig {
    pub schema_version: u32,
    #[serde(default)]
    pub project: ProjectCfg,
    #[serde(default)]
    pub spine: SpineCfg,
    #[serde(default)]
    pub hashing: HashingCfg,
    #[serde(default)]
    pub layers: BTreeMap<String, LayerCfg>,
    #[serde(default)]
    pub generators: BTreeMap<String, GeneratorCfg>,
    #[serde(default)]
    pub stores: BTreeMap<String, StoreCfg>,
    #[serde(default)]
    pub coordination: CoordinationCfg,
    #[serde(default)]
    pub sidecar: SidecarCfg,
}

impl RawConfig {
    pub fn defaults() -> Self {
        let mut profiles = BTreeMap::new();
        profiles.insert("structure".to_string(), HashProfileCfg::default());
        Self {
            schema_version: CURRENT_SCHEMA_VERSION,
            project: ProjectCfg::default(),
            spine: SpineCfg::default(),
            hashing: HashingCfg { profiles },
            layers: BTreeMap::new(),
            generators: BTreeMap::new(),
            stores: BTreeMap::new(),
            coordination: CoordinationCfg::default(),
            sidecar: SidecarCfg::default(),
        }
    }

    pub fn load(sidecar: &Sidecar) -> Result<Self, ConfigError> {
        if !sidecar.config_path().exists() {
            return Ok(Self::defaults());
        }

        let committed = std::fs::read_to_string(sidecar.config_path())
            .map_err(|e| ConfigError::Parse(e.to_string()))?;
        let mut value: toml::Value =
            toml::from_str(&committed).map_err(|e| ConfigError::Parse(e.to_string()))?;

        if sidecar.config_local_path().exists() {
            let local = std::fs::read_to_string(sidecar.config_local_path())
                .map_err(|e| ConfigError::Parse(e.to_string()))?;
            let local_value: toml::Value =
                toml::from_str(&local).map_err(|e| ConfigError::Parse(e.to_string()))?;
            shallow_merge(&mut value, local_value);
        }

        let secrets = load_secrets(sidecar)?;
        expand_value(&mut value, &secrets)?;

        let mut cfg: Self = value
            .try_into()
            .map_err(|e: toml::de::Error| ConfigError::Parse(e.to_string()))?;

        // A partial config.toml (e.g. one that only sets `[spine] retain_cap`)
        // deserialises `hashing.profiles` as an empty map, which would leave the
        // default `spine.default_hash_profile = "structure"` dangling.  Seed the
        // built-in `structure` profile that `defaults()` provides so a partial
        // config inherits it; an *explicitly* bad reference
        // (`default_hash_profile = "nope"`) still dangles in `validate`.
        cfg.hashing
            .profiles
            .entry("structure".to_string())
            .or_insert_with(HashProfileCfg::default);

        Ok(cfg)
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProjectCfg {
    pub name: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SpineCfg {
    #[serde(default = "default_retain_cap")]
    pub retain_cap: usize,
    #[serde(default = "default_hash_profile")]
    pub default_hash_profile: String,
}

impl Default for SpineCfg {
    fn default() -> Self {
        Self {
            retain_cap: default_retain_cap(),
            default_hash_profile: default_hash_profile(),
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct HashingCfg {
    #[serde(default)]
    pub profiles: BTreeMap<String, HashProfileCfg>,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct HashProfileCfg {
    #[serde(default = "default_true")]
    pub whitespace_insensitive: bool,
    #[serde(default = "default_true")]
    pub normalize_trailing_commas: bool,
    #[serde(default)]
    pub include_comments: bool,
    #[serde(default)]
    pub include_docstrings: bool,
}

impl Default for HashProfileCfg {
    fn default() -> Self {
        Self {
            whitespace_insensitive: true,
            normalize_trailing_commas: true,
            include_comments: false,
            include_docstrings: false,
        }
    }
}

impl HashProfileCfg {
    pub fn matches_hash_policy_default(&self) -> bool {
        let policy = HashPolicy::default();
        self.whitespace_insensitive == policy.ignore_whitespace
            && self.normalize_trailing_commas == policy.ignore_trailing_comma
            && self.include_comments == policy.include_comments
            && self.include_docstrings == policy.include_docstrings
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LayerOrigin {
    Derived,
    Authored,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ServingMode {
    Stale,
    Block,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RecomputeMode {
    Lazy,
    Eager,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LayerCfg {
    pub origin: LayerOrigin,
    #[serde(default)]
    pub depends_on: Vec<String>,
    pub generator: Option<String>,
    pub generator_version: Option<String>,
    pub hash_profile: Option<String>,
    pub store: Option<String>,
    #[serde(default = "default_serving")]
    pub serving: ServingMode,
    #[serde(default = "default_recompute")]
    pub recompute: RecomputeMode,
    #[serde(default)]
    pub entity_kinds: Vec<String>,
    #[serde(default = "default_true")]
    pub history: bool,
    #[serde(default = "default_true")]
    pub review_on_change: bool,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GeneratorCfg {
    #[serde(rename = "type")]
    pub generator_type: Option<String>,
    pub callable: Option<String>,
    #[serde(default)]
    pub command: Vec<String>,
    pub endpoint: Option<String>,
    pub model: Option<String>,
    pub dim: Option<usize>,
    pub batch_size: Option<usize>,
    pub concurrency: Option<usize>,
    pub timeout_ms: Option<u64>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum GcPolicy {
    Orphans,
    Never,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StoreCfg {
    pub backend: String,
    pub path: Option<String>,
    pub url: Option<String>,
    pub metric: Option<String>,
    pub dim: Option<usize>,
    #[serde(default = "default_gc")]
    pub gc: GcPolicy,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ValidatedConfig {
    pub raw: RawConfig,
    pub topo_order: Vec<String>,
}

impl ValidatedConfig {
    pub fn profile_for(&self, layer: &str) -> &HashProfileCfg {
        if layer == "code" {
            return &self.raw.hashing.profiles[&self.raw.spine.default_hash_profile];
        }
        let layer_cfg = &self.raw.layers[layer];
        let profile = layer_cfg
            .hash_profile
            .as_deref()
            .unwrap_or(&self.raw.spine.default_hash_profile);
        &self.raw.hashing.profiles[profile]
    }

    pub fn hash_policy_for(&self, layer: &str) -> HashPolicy {
        HashPolicy::from(self.profile_for(layer))
    }

    /// Return a reference to the `LayerCfg` for a declared authored layer,
    /// or `None` if the layer is not declared or not authored.
    pub fn authored_layer_config(&self, layer: &str) -> Option<&LayerCfg> {
        let lc = self.raw.layers.get(layer)?;
        if matches!(lc.origin, LayerOrigin::Authored) {
            Some(lc)
        } else {
            None
        }
    }
}

pub fn validate(raw: RawConfig) -> Result<ValidatedConfig, ConfigError> {
    if raw.schema_version != CURRENT_SCHEMA_VERSION {
        return Err(ConfigError::UnknownVersion(raw.schema_version));
    }

    if !raw
        .hashing
        .profiles
        .contains_key(&raw.spine.default_hash_profile)
    {
        return Err(ConfigError::DanglingRef {
            key: "spine.default_hash_profile".to_string(),
            target: raw.spine.default_hash_profile.clone(),
        });
    }

    for (name, layer) in &raw.layers {
        let profile = layer
            .hash_profile
            .as_deref()
            .unwrap_or(&raw.spine.default_hash_profile);
        if !raw.hashing.profiles.contains_key(profile) {
            return Err(ConfigError::DanglingRef {
                key: format!("layers.{name}.hash_profile"),
                target: profile.to_string(),
            });
        }
    }

    for (name, layer) in &raw.layers {
        if matches!(layer.origin, LayerOrigin::Derived) {
            let Some(generator) = &layer.generator else {
                return Err(ConfigError::OriginViolation(format!(
                    "layers.{name}.generator is required for derived layers"
                )));
            };
            if !raw.generators.contains_key(generator) {
                return Err(ConfigError::DanglingRef {
                    key: format!("layers.{name}.generator"),
                    target: generator.clone(),
                });
            }
            let Some(store) = &layer.store else {
                return Err(ConfigError::OriginViolation(format!(
                    "layers.{name}.store is required for derived layers"
                )));
            };
            if !raw.stores.contains_key(store) {
                return Err(ConfigError::DanglingRef {
                    key: format!("layers.{name}.store"),
                    target: store.clone(),
                });
            }
        }
    }

    if raw.layers.contains_key("code") {
        return Err(ConfigError::ReservedName("code".to_string()));
    }

    let topo_order = topo_order(&raw)?;

    for (name, layer) in &raw.layers {
        for dep in effective_depends_on(layer) {
            if let Some(dep_layer) = raw.layers.get(&dep) {
                if matches!(dep_layer.origin, LayerOrigin::Authored) {
                    return Err(ConfigError::OriginViolation(format!(
                        "layers.{name}.depends_on references authored layer {dep}"
                    )));
                }
            }
        }
    }

    for (name, layer) in &raw.layers {
        match layer.origin {
            LayerOrigin::Derived => {
                if layer.generator_version.as_deref().unwrap_or("").is_empty() {
                    return Err(ConfigError::OriginViolation(format!(
                        "layers.{name}.generator_version is required for derived layers"
                    )));
                }
            }
            LayerOrigin::Authored => {
                if !layer.depends_on.is_empty()
                    || layer.generator.is_some()
                    || layer.generator_version.is_some()
                    || layer.hash_profile.is_some()
                    || layer.store.is_some()
                {
                    return Err(ConfigError::OriginViolation(format!(
                        "layers.{name} is authored but defines derived-only keys"
                    )));
                }
            }
        }
    }

    for (name, layer) in &raw.layers {
        for kind in &layer.entity_kinds {
            if !is_known_kind(kind) {
                return Err(ConfigError::UnknownKind(format!(
                    "layers.{name}.entity_kinds contains {kind}"
                )));
            }
        }
    }

    for (name, layer) in &raw.layers {
        if let (Some(generator), Some(store)) = (&layer.generator, &layer.store) {
            let generator_dim = raw.generators.get(generator).and_then(|g| g.dim);
            let store_dim = raw.stores.get(store).and_then(|s| s.dim);
            if let (Some(generator_dim), Some(store_dim)) = (generator_dim, store_dim) {
                if generator_dim != store_dim {
                    return Err(ConfigError::DimMismatch(format!(
                        "layers.{name}: generator {generator} dim {generator_dim} != store {store} dim {store_dim}"
                    )));
                }
            }
        }
    }

    // Coordination: reject a writer-blocking overflow policy at open (§5.11 —
    // a slow or dead subscriber MUST NOT stall the writer). Only the three
    // non-blocking policies are accepted; fail loudly, never a silent default.
    match raw.coordination.bus.overflow.as_str() {
        "coalesce" | "drop_and_mark_lagged" | "error_and_close" => {}
        other => return Err(ConfigError::BlockingOverflow(other.to_string())),
    }

    lint_secrets(&raw)?;

    Ok(ValidatedConfig { raw, topo_order })
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CoordinationCfg {
    #[serde(default)]
    pub bus: BusCfg,
    #[serde(default)]
    pub watcher: WatcherCfg,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct BusCfg {
    #[serde(default = "default_queue_capacity")]
    pub queue_capacity: usize,
    #[serde(default = "default_overflow")]
    pub overflow: String,
}

impl Default for BusCfg {
    fn default() -> Self {
        Self {
            queue_capacity: default_queue_capacity(),
            overflow: default_overflow(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WatcherCfg {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_debounce_ms")]
    pub debounce_ms: u64,
}

impl Default for WatcherCfg {
    fn default() -> Self {
        Self {
            enabled: false,
            debounce_ms: default_debounce_ms(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SidecarCfg {
    #[serde(default = "default_true")]
    pub gitignore_cache: bool,
}

impl Default for SidecarCfg {
    fn default() -> Self {
        Self {
            gitignore_cache: true,
        }
    }
}

fn shallow_merge(base: &mut toml::Value, overlay: toml::Value) {
    match (base, overlay) {
        (toml::Value::Table(base_table), toml::Value::Table(overlay_table)) => {
            for (key, value) in overlay_table {
                base_table.insert(key, value);
            }
        }
        (base_slot, overlay_value) => *base_slot = overlay_value,
    }
}

fn load_secrets(sidecar: &Sidecar) -> Result<BTreeMap<String, String>, ConfigError> {
    if !sidecar.secrets_path().exists() {
        return Ok(BTreeMap::new());
    }
    let text = std::fs::read_to_string(sidecar.secrets_path())
        .map_err(|e| ConfigError::Parse(e.to_string()))?;
    let value: toml::Value = toml::from_str(&text).map_err(|e| ConfigError::Parse(e.to_string()))?;
    let mut secrets = BTreeMap::new();
    if let toml::Value::Table(table) = value {
        for (key, value) in table {
            if let toml::Value::String(s) = value {
                secrets.insert(key, s);
            }
        }
    }
    Ok(secrets)
}

fn expand_value(
    value: &mut toml::Value,
    secrets: &BTreeMap<String, String>,
) -> Result<(), ConfigError> {
    match value {
        toml::Value::String(s) => {
            *s = expand_string(s, secrets)?;
        }
        toml::Value::Array(items) => {
            for item in items {
                expand_value(item, secrets)?;
            }
        }
        toml::Value::Table(table) => {
            for (_, item) in table.iter_mut() {
                expand_value(item, secrets)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn expand_string(s: &str, secrets: &BTreeMap<String, String>) -> Result<String, ConfigError> {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(start) = rest.find("${") {
        out.push_str(&rest[..start]);
        let after = &rest[start + 2..];
        let Some(end) = after.find('}') else {
            out.push_str(&rest[start..]);
            return Ok(out);
        };
        let name = &after[..end];
        let value = std::env::var(name)
            .ok()
            .or_else(|| secrets.get(name).cloned())
            .ok_or_else(|| ConfigError::UndefinedEnv(name.to_string()))?;
        out.push_str(&value);
        rest = &after[end + 1..];
    }
    out.push_str(rest);
    Ok(out)
}

fn default_true() -> bool {
    true
}

fn default_retain_cap() -> usize {
    256
}

fn default_hash_profile() -> String {
    "structure".to_string()
}

fn default_serving() -> ServingMode {
    ServingMode::Stale
}

fn default_recompute() -> RecomputeMode {
    RecomputeMode::Lazy
}

fn default_gc() -> GcPolicy {
    GcPolicy::Never
}

fn default_queue_capacity() -> usize {
    1024
}

fn default_overflow() -> String {
    "coalesce".to_string()
}

fn default_debounce_ms() -> u64 {
    200
}

fn effective_depends_on(layer: &LayerCfg) -> Vec<String> {
    if matches!(layer.origin, LayerOrigin::Derived) && layer.depends_on.is_empty() {
        vec!["code".to_string()]
    } else {
        layer.depends_on.clone()
    }
}

fn topo_order(raw: &RawConfig) -> Result<Vec<String>, ConfigError> {
    let mut incoming: BTreeMap<String, usize> = BTreeMap::new();
    let mut outgoing: BTreeMap<String, Vec<String>> = BTreeMap::new();

    incoming.insert("code".to_string(), 0);
    for name in raw.layers.keys() {
        incoming.insert(name.clone(), 0);
    }

    for (name, layer) in &raw.layers {
        for dep in effective_depends_on(layer) {
            if dep != "code" && !raw.layers.contains_key(&dep) {
                return Err(ConfigError::DanglingRef {
                    key: format!("layers.{name}.depends_on"),
                    target: dep,
                });
            }
            *incoming.get_mut(name).expect("layer was inserted") += 1;
            outgoing.entry(dep).or_default().push(name.clone());
        }
    }

    for targets in outgoing.values_mut() {
        targets.sort();
    }

    let mut ready: Vec<String> = incoming
        .iter()
        .filter_map(|(name, count)| (*count == 0).then(|| name.clone()))
        .collect();
    ready.sort();

    let mut order = Vec::with_capacity(incoming.len());
    while let Some(node) = ready.first().cloned() {
        ready.remove(0);
        order.push(node.clone());

        if let Some(targets) = outgoing.get(&node) {
            for target in targets {
                let count = incoming.get_mut(target).expect("target exists");
                *count -= 1;
                if *count == 0 {
                    ready.push(target.clone());
                    ready.sort();
                }
            }
        }
    }

    if order.len() != incoming.len() {
        let remaining: Vec<String> = incoming
            .into_iter()
            .filter_map(|(name, count)| (count > 0).then_some(name))
            .collect();
        return Err(ConfigError::Cycle(remaining.join(" -> ")));
    }

    Ok(order)
}

fn is_known_kind(kind: &str) -> bool {
    matches!(
        kind,
        "module"
            | "class"
            | "function"
            | "method"
            | "constructor"
            | "variable"
            | "constant"
            | "field"
            | "parameter"
            | "property"
            | "type_parameter"
            | "import"
    )
}

fn lint_secrets(raw: &RawConfig) -> Result<(), ConfigError> {
    let value = serde_json::to_value(raw).map_err(|e| ConfigError::Parse(e.to_string()))?;
    lint_secret_value("$", &value)
}

fn lint_secret_value(path: &str, value: &serde_json::Value) -> Result<(), ConfigError> {
    match value {
        serde_json::Value::String(s) => {
            if looks_like_secret(s) {
                return Err(ConfigError::SecretInline(path.to_string()));
            }
        }
        serde_json::Value::Array(items) => {
            for (idx, item) in items.iter().enumerate() {
                lint_secret_value(&format!("{path}[{idx}]"), item)?;
            }
        }
        serde_json::Value::Object(map) => {
            for (key, item) in map {
                lint_secret_value(&format!("{path}.{key}"), item)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn looks_like_secret(s: &str) -> bool {
    s.starts_with("sk-")
        || s.starts_with("AKIA")
        || (s.len() >= 32 && high_entropyish(s))
}

fn high_entropyish(s: &str) -> bool {
    let alnum = s.chars().filter(|c| c.is_ascii_alphanumeric()).count();
    let has_lower = s.chars().any(|c| c.is_ascii_lowercase());
    let has_upper = s.chars().any(|c| c.is_ascii_uppercase());
    let has_digit = s.chars().any(|c| c.is_ascii_digit());
    alnum >= 28 && has_lower && has_upper && has_digit
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sidecar::Sidecar;

    const FULL_EXAMPLE: &str = r#"
schema_version = 1

[project]
name = "mylib"

[spine]
retain_cap = 256
default_hash_profile = "structure"

[hashing.profiles.structure]
whitespace_insensitive = true
normalize_trailing_commas = true
include_comments = false
include_docstrings = false

[hashing.profiles.semantic]
whitespace_insensitive = true
normalize_trailing_commas = true
include_comments = true
include_docstrings = true

[layers.embeddings]
origin = "derived"
depends_on = ["code"]
generator = "openai_embed"
generator_version = "text-embedding-3-large@v1"
hash_profile = "semantic"
store = "vectors"
serving = "stale"
recompute = "lazy"
entity_kinds = ["function", "method", "class", "module"]

[layers.docstrings]
origin = "derived"
depends_on = ["code"]
generator = "extract_docstring"
generator_version = "v1"
hash_profile = "structure"
store = "kv_docstrings"
serving = "stale"

[layers.descriptions]
origin = "derived"
depends_on = ["code"]
generator = "llm_describe"
generator_version = "claude@v1"
hash_profile = "semantic"
store = "kv_descriptions"
serving = "stale"
recompute = "eager"

[layers.description_embeddings]
origin = "derived"
depends_on = ["descriptions"]
generator = "openai_embed"
generator_version = "text-embedding-3-large@v1"
hash_profile = "semantic"
store = "vectors"

[layers.intent]
origin = "authored"
history = true
review_on_change = true

[generators.openai_embed]
type = "http"
endpoint = "https://api.example/embeddings"
model = "text-embedding-3-large"
dim = 3072
batch_size = 128
concurrency = 4
timeout_ms = 30000

[generators.extract_docstring]
type = "python"
callable = "mylib.tyo3_gen:extract_docstring"

[generators.llm_describe]
type = "command"
command = ["tyo3-describe", "--model", "claude"]
timeout_ms = 60000
concurrency = 2

[stores.vectors]
backend = "lancedb"
path = "cache/embeddings"
metric = "cosine"
gc = "never"

[stores.kv_docstrings]
backend = "fs"
path = "cache/docstrings"

[stores.kv_descriptions]
backend = "fs"
path = "cache/descriptions"

[coordination.bus]
queue_capacity = 1024
overflow = "coalesce"

[coordination.watcher]
enabled = false
debounce_ms = 200

[sidecar]
gitignore_cache = true
"#;

    #[test]
    fn full_annotated_example_deserialises() {
        let cfg: RawConfig = toml::from_str(FULL_EXAMPLE).unwrap();
        assert_eq!(cfg.schema_version, 1);
        assert!(cfg.layers.contains_key("embeddings"));
        assert_eq!(cfg.stores["vectors"].gc, GcPolicy::Never);
    }

    #[test]
    fn missing_file_yields_defaults_matching_gate2_hash_policy() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = RawConfig::load(&Sidecar::new(dir.path())).unwrap();
        let structure = cfg.hashing.profiles.get("structure").unwrap();

        assert_eq!(cfg.schema_version, CURRENT_SCHEMA_VERSION);
        assert_eq!(cfg.spine.retain_cap, 256);
        assert!(structure.matches_hash_policy_default());
    }

    /// A partial config.toml that sets only `[spine] retain_cap` must still load
    /// and validate: the built-in `structure` profile is seeded so the default
    /// `spine.default_hash_profile` does not dangle.
    #[test]
    fn partial_config_seeds_default_hash_profile_and_validates() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = Sidecar::new(dir.path());
        std::fs::create_dir_all(dir.path().join(".tyo3")).unwrap();
        std::fs::write(
            sidecar.config_path(),
            "schema_version = 1\n\n[spine]\nretain_cap = 4\n",
        )
        .unwrap();

        let cfg = RawConfig::load(&sidecar).unwrap();
        assert_eq!(cfg.spine.retain_cap, 4);
        assert!(
            cfg.hashing.profiles.contains_key("structure"),
            "partial config must inherit the built-in `structure` profile"
        );
        // The whole config validates (no dangling default_hash_profile).
        assert!(validate(cfg).is_ok(), "partial config must validate");
    }

    #[test]
    fn unknown_top_level_key_errors() {
        let err = toml::from_str::<RawConfig>("schema_version = 1\nwat = true\n").unwrap_err();
        assert!(err.to_string().contains("unknown field"));
    }

    #[test]
    fn config_local_shallow_overlay_replaces_store_table() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = Sidecar::new(dir.path());
        std::fs::create_dir_all(dir.path().join(".tyo3")).unwrap();
        std::fs::write(
            sidecar.config_path(),
            r#"
schema_version = 1
[hashing.profiles.structure]
[stores.vectors]
backend = "fs"
path = "cache/shared"
"#,
        )
        .unwrap();
        std::fs::write(
            sidecar.config_local_path(),
            r#"
[stores.vectors]
backend = "fs"
path = "cache/local"
"#,
        )
        .unwrap();

        let cfg = RawConfig::load(&sidecar).unwrap();

        assert_eq!(cfg.stores["vectors"].path.as_deref(), Some("cache/local"));
        assert!(cfg.hashing.profiles.contains_key("structure"));
    }

    #[test]
    fn expands_env_var_and_errors_on_missing_var() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = Sidecar::new(dir.path());
        std::fs::create_dir_all(dir.path().join(".tyo3")).unwrap();
        std::env::set_var("TYO3_TEST_VAR", "expanded");
        std::fs::write(
            sidecar.config_path(),
            r#"
schema_version = 1
[hashing.profiles.structure]
[stores.local]
backend = "fs"
path = "${TYO3_TEST_VAR}"
"#,
        )
        .unwrap();

        let cfg = RawConfig::load(&sidecar).unwrap();
        assert_eq!(cfg.stores["local"].path.as_deref(), Some("expanded"));

        std::fs::write(
            sidecar.config_path(),
            r#"
schema_version = 1
[hashing.profiles.structure]
[stores.local]
backend = "fs"
path = "${MISSING_TYO3_TEST_VAR}"
"#,
        )
        .unwrap();

        let err = RawConfig::load(&sidecar).unwrap_err();
        assert!(matches!(err, ConfigError::UndefinedEnv(_)));
        std::env::remove_var("TYO3_TEST_VAR");
    }

    fn parse_cfg(text: &str) -> RawConfig {
        toml::from_str(text).unwrap()
    }

    fn valid_layered_config() -> RawConfig {
        parse_cfg(
            r#"
schema_version = 1

[hashing.profiles.structure]

[hashing.profiles.semantic]
include_comments = true
include_docstrings = true

[layers.descriptions]
origin = "derived"
depends_on = ["code"]
generator = "describe"
generator_version = "v1"
store = "kv"
hash_profile = "semantic"
entity_kinds = ["function"]

[layers.description_embeddings]
origin = "derived"
depends_on = ["descriptions"]
generator = "embed"
generator_version = "v1"
store = "vectors"
hash_profile = "semantic"

[generators.describe]
type = "python"
callable = "pkg:describe"

[generators.embed]
type = "http"
endpoint = "https://example.invalid/embed"
dim = 3

[stores.kv]
backend = "fs"
path = "cache/descriptions"

[stores.vectors]
backend = "fs"
path = "cache/vectors"
dim = 3
"#,
        )
    }

    #[test]
    fn valid_full_example_topo_order_is_precomputed() {
        let cfg = validate(valid_layered_config()).unwrap();
        assert_eq!(
            cfg.topo_order,
            vec![
                "code".to_string(),
                "descriptions".to_string(),
                "description_embeddings".to_string()
            ]
        );
        assert!(cfg.profile_for("descriptions").include_docstrings);
    }

    #[test]
    fn hash_policy_for_code_uses_default_profile() {
        let cfg = validate(valid_layered_config()).unwrap();
        let policy = cfg.hash_policy_for("code");

        assert_eq!(policy, HashPolicy::default());
    }

    #[test]
    fn default_hash_profile_must_resolve() {
        let mut raw = valid_layered_config();
        raw.spine.default_hash_profile = "nope".to_string();

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::DanglingRef { key, target } if key == "spine.default_hash_profile" && target == "nope"));
    }

    #[test]
    fn depends_on_must_resolve() {
        let mut raw = valid_layered_config();
        raw.layers
            .get_mut("descriptions")
            .unwrap()
            .depends_on = vec!["missing".to_string()];

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::DanglingRef { key, target } if key == "layers.descriptions.depends_on" && target == "missing"));
    }

    #[test]
    fn cycles_are_rejected() {
        let mut raw = valid_layered_config();
        raw.layers.get_mut("descriptions").unwrap().depends_on =
            vec!["description_embeddings".to_string()];
        raw.layers
            .get_mut("description_embeddings")
            .unwrap()
            .depends_on = vec!["descriptions".to_string()];

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::Cycle(cycle) if cycle.contains("descriptions")));
    }

    #[test]
    fn authored_layer_cannot_be_a_dependency() {
        let mut raw = valid_layered_config();
        raw.layers.insert(
            "intent".to_string(),
            LayerCfg {
                origin: LayerOrigin::Authored,
                depends_on: Vec::new(),
                generator: None,
                generator_version: None,
                hash_profile: None,
                store: None,
                serving: ServingMode::Stale,
                recompute: RecomputeMode::Lazy,
                entity_kinds: Vec::new(),
                history: true,
                review_on_change: true,
            },
        );
        raw.layers
            .get_mut("descriptions")
            .unwrap()
            .depends_on = vec!["intent".to_string()];

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::OriginViolation(s) if s.contains("authored layer intent")));
    }

    #[test]
    fn code_is_reserved_layer_name() {
        let mut raw = valid_layered_config();
        raw.layers.insert(
            "code".to_string(),
            LayerCfg {
                origin: LayerOrigin::Authored,
                depends_on: Vec::new(),
                generator: None,
                generator_version: None,
                hash_profile: None,
                store: None,
                serving: ServingMode::Stale,
                recompute: RecomputeMode::Lazy,
                entity_kinds: Vec::new(),
                history: true,
                review_on_change: true,
            },
        );

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::ReservedName(name) if name == "code"));
    }

    #[test]
    fn derived_layer_requires_store() {
        let mut raw = valid_layered_config();
        raw.layers.get_mut("descriptions").unwrap().store = None;

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::OriginViolation(s) if s.contains("store is required")));
    }

    #[test]
    fn entity_kinds_are_pinned_to_symbol_kind() {
        let mut raw = valid_layered_config();
        raw.layers
            .get_mut("descriptions")
            .unwrap()
            .entity_kinds = vec!["wizard".to_string()];

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::UnknownKind(s) if s.contains("wizard")));
    }

    #[test]
    fn inline_secret_like_values_are_rejected() {
        let mut raw = valid_layered_config();
        raw.generators.get_mut("embed").unwrap().endpoint =
            Some("sk-livesecret1234567890".to_string());

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::SecretInline(path) if path.contains("endpoint")));
    }

    #[test]
    fn dim_mismatch_is_rejected_when_both_sides_set() {
        let mut raw = valid_layered_config();
        raw.stores.get_mut("vectors").unwrap().dim = Some(4);

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::DimMismatch(s) if s.contains("dim 3") && s.contains("dim 4")));
    }

    #[test]
    fn schema_version_must_be_supported() {
        let mut raw = valid_layered_config();
        raw.schema_version = 999;

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::UnknownVersion(999)));
    }

    #[test]
    fn writer_blocking_overflow_policy_is_rejected() {
        let mut raw = valid_layered_config();
        raw.coordination.bus.overflow = "block".to_string();

        let err = validate(raw).unwrap_err();

        assert!(matches!(err, ConfigError::BlockingOverflow(p) if p == "block"));
    }

    #[test]
    fn non_blocking_overflow_policies_are_accepted() {
        for policy in ["coalesce", "drop_and_mark_lagged", "error_and_close"] {
            let mut raw = valid_layered_config();
            raw.coordination.bus.overflow = policy.to_string();
            assert!(
                validate(raw).is_ok(),
                "policy {policy} should be accepted"
            );
        }
    }

    #[test]
    fn validation_topo_order_is_deterministic() {
        let first = validate(valid_layered_config()).unwrap().topo_order;
        let second = validate(valid_layered_config()).unwrap().topo_order;

        assert_eq!(first, second);
    }
}
