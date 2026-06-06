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
    Parse(String),
    UndefinedEnv(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::Parse(s) => write!(f, "config parse error: {s}"),
            ConfigError::UndefinedEnv(name) => {
                write!(f, "undefined environment variable in config: {name}")
            }
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

        value
            .try_into()
            .map_err(|e: toml::de::Error| ConfigError::Parse(e.to_string()))
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
    #[serde(default = "default_gc")]
    pub gc: GcPolicy,
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
}
