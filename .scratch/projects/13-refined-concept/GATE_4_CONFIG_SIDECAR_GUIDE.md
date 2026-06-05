# Gate 4 — Configuration & Sidecar Foundation: Implementation Guide

> Implements **REFINED_SPEC.md §11** (the `.tyo3/` sidecar and persistence
> round-trip, beyond Gate 2's `identity.db` slice), the **static** half of **§9.2.2**
> (the `depends_on` graph is acyclic — checked at config-load time), **§7.2.2**
> (named hashing profiles drive the content-hash normalisation policy), and the
> **§13.1** typed-error discipline (a unified `FormatVersion`/config error model).
> It also promotes `config_schema_proposal.md` from *proposal* to *normative*.
>
> This is the **declarative + storage foundation** every later gate sits on:
> derived layers (Gate 5), authored layers (Gate 6), cross-layer reads (Gate 7),
> and the bus/watcher (Gate 8) all read the config and write under the sidecar this
> gate establishes. It is deliberately **not** a behavioural gate — it adds **no
> generation, no new write type, and no change to the commit transaction**. Its
> job is to lock the contracts the next four gates plug into so they never have to
> be re-cut.
>
> **Prerequisites:** `gate3-complete` tag must exist. This gate consumes Gate 2's
> `HashPolicy` and `IdentityRegistry` persistence and Gate 1's `ContentStore`.
>
> **Audience:** an engineer new to the codebase. Same working rules as Gates 1–3:
> `devenv shell -- <script>` for everything (never bare `cargo`/`pytest`); one
> labelled commit per validated step (`gate4: step N — …`); never skip a validation.

## How to work in this repo

- Rust unit tests: `devenv shell -- test-rust`.
- Full suite (Rust + Python): `devenv shell -- tests`.
- Rebuild the native module after Rust changes: `devenv shell -- build`
  (required before the Python side sees new PyO3 symbols).
- Work on a branch off `gate3-complete`. Commit after each step's validation passes.

## The contract you are building toward (read first)

Four contracts, each finalised here so no later gate redefines them:

1. **One config, validated, single source of truth (SPEC §11.2, §9.2.2 static).**
   `.tyo3/config.toml` is parsed and validated **once, in Rust**, at session open.
   An invalid config (dangling reference, dependency cycle, authored-as-dependency,
   reserved name, unknown `schema_version`) is rejected with a **typed, catchable
   error before the session opens** — never silently misread.
2. **One owner of `.tyo3/` (SPEC §11.3.3, §11.3.4).** A single `Sidecar` type owns
   the directory layout and all crash-safe writes. Nothing else in the codebase
   constructs a `.tyo3/...` path by hand. The source tree is never touched
   (§11.3.4); a collaborator without TyO3 sees one inert directory (§11.3.6).
3. **Profiles drive hashing (SPEC §7.2.2).** Named `[hashing.profiles.*]` entries
   feed Gate 2's `HashPolicy`. **When no config exists, the synthesized defaults
   reproduce today's Gate-2 hashing exactly** — Gate 4 is a no-op for an
   un-configured project.
4. **One store interface (SPEC §8.2.6, §11.2 `cache/`).** A `Store` / `VectorStore`
   protocol fixes the KV-vs-vector boundary and delegates ANN to external backends.
   Gate 4 ships the protocol, the `fs` backend, and the `cache/<layer>/` scaffolding;
   real vector backends are lazily imported so the build never depends on them.

The defining success property: **opening a project with a full, valid
`config.toml` succeeds and exposes the validated config; opening with any invalid
config fails with the correct typed error; opening with no config behaves exactly
as Gate 3 did.**

## Design stance (read before you start)

We optimise for the cleanest end-state architecture, not for minimal diffs.
Where introducing config and the sidecar exposes a rough edge in Gate 1–3 code —
a hardcoded path, a constructor that should take a parameter, a format concern
tangled with I/O, a duplicated exception hierarchy — **refactor it properly here**
rather than working around it. The Gate 1–3 test suites are the safety net: they
let you refactor confidently, and a test whose *meaning* changes because the design
genuinely improved is a deliberate, reviewable edit, not a violation. The only hard
rule is the **no-config-equals-Gate-3** behaviour (§11.3.6): an un-configured
project must be byte-for-byte unaffected. Everything else is fair game to make
cleaner.

## Why this is the right place in the sequence

Everything above the spine is *declared* in `config.toml` and *stored* under
`.tyo3/`. If we built derived layers (Gate 5) before finalising the schema and the
sidecar, every layer would carry an ad-hoc declaration and an ad-hoc storage path
that we would then have to migrate. Doing it first means Gates 5–8 are purely
additive: they read an already-final config and write under an already-final
sidecar. The two "cut-once" seams this gate leaves for Gate 5 — *the validated DAG
of layers* and *the store interface* — are described in **"Carrying forward to
Gate 5"** at the end; treat them as deliverables, not background.

## Target module layout

```
rust/src/
  sidecar.rs     # NEW: Sidecar — owns .tyo3/ layout + crash-safe writes (Step 1)
  config.rs      # NEW: typed config model, TOML parse, validation (Steps 2–4)
  hash.rs        # wire HashPolicy from a resolved profile (Step 4)
  project.rs     # load config on open; route identity.db via Sidecar; expose config (Steps 1, 5)
  lib.rs         # register ConfigError + FormatVersionError (Step 8)
src/tyo3/
  config.py      # NEW: frozen dataclass mirror of the validated config (Step 5)
  sidecar.py     # NEW: authored/ + cache/ layout helpers, .gitignore writer (Step 7)
  stores/        # NEW: Store/VectorStore protocols + fs backend + registry (Step 6)
    __init__.py
    base.py
    fs.py
  exceptions.py  # add ConfigError, FormatVersionError mirrors (Step 8)
  session.py     # load + expose session.config; ensure sidecar on open (Step 5)
.scratch/projects/13-refined-concept/
  config_schema_proposal.md  → promoted to CONFIG_SCHEMA.md (Step 0)
```

Keep each new Rust type's `// INVARIANT:` / `// CONCURRENCY:` comments accurate;
they are part of the deliverable, as in Gates 1–3.

---

## Step 0 — Lock the schema: resolve open questions, promote to normative

**Goal.** Turn `config_schema_proposal.md` into the normative `CONFIG_SCHEMA.md`
the rest of this gate implements, by deciding its six open questions. No code; this
step produces the spec the validator (Step 3) is held to. (Mirrors Gate 3 Step 0:
characterise before you build.)

**Files.** Rename `config_schema_proposal.md` → `CONFIG_SCHEMA.md`; change its
`Status:` line to *normative as of Gate 4*; record the decisions below; update the
README's document list if it references the proposal.

**Decisions (adopt these unless the team overrides — record any override in the
commit message).**

1. **Secrets.** Config holds **no** secrets. A value may reference an environment
   variable with `${VAR}` syntax, expanded at load. An optional, **gitignored**
   `.tyo3/secrets.toml` MAY supply env values for local dev. The load-time secrets
   lint (Step 3 rule 8) rejects any value that looks like an inline credential
   (long high-entropy string, `sk-…`, `AKIA…`, etc.) with a `ConfigError`.
2. **Per-store GC.** Add `[stores.<name>] gc = "orphans" | "never"` (default
   `"never"`). Gate 4 parses and validates the knob; the **actual** orphan-eviction
   pass is deferred to Gate 5/9. Document this.
3. **`generator_version` ergonomics.** Keep it a **manual, required string**
   (legible in diffs). No auto-derivation. (Validation may later warn on a changed
   generator config with an unchanged version — deferred.)
4. **Profile inheritance.** Profiles stay **flat and explicit** — no `extends`.
   Revisit only if duplication becomes painful.
5. **Environment overlays.** Support an optional, non-committed
   `.tyo3/config.local.toml` that **shallow-merges over** the committed config
   (per-table override; local wins). Common need: point `stores.vectors.path` at a
   developer-local directory. Document the precedence precisely.
6. **`entity_kinds` vocabulary.** Pin to Gate 2's `SymbolKind` variants as the
   canonical enum. Validation rejects any kind not in that list.

**Validate.**
- `CONFIG_SCHEMA.md` exists, marked normative, with the six decisions recorded and
  the `${VAR}` and `config.local.toml` rules added to the field reference.
- **Acceptance gate:** the document is internally consistent (every key referenced
  by the validation rules in §3 of the schema exists in the field reference).
  Commit (`gate4: step 0 — config schema normative; open questions resolved`).

> Do not start coding the validator before this is locked. The validator is only as
> correct as the schema it enforces.

---

## Step 1 — The `Sidecar` type: single owner of `.tyo3/`

**Goal.** One type owns the `.tyo3/` directory layout and every write into it, so
no later gate hand-builds a sidecar path and no write is non-atomic (SPEC §11.2,
§11.3.3, §11.3.4). Move Gate 2's `identity.db` persistence onto it and split format
from I/O while you're there.

**Files.** `rust/src/sidecar.rs` (new); `mod sidecar;` in `lib.rs`; refactor the
three hardcoded `.tyo3/identity.db` paths in `project.rs` to go through it.

**Build.**
```rust
//! Owns the on-disk `.tyo3/` sidecar layout (SPEC §11.2) and all crash-safe
//! writes into it. The single place any `.tyo3/...` path is constructed.
//
// INVARIANT: every write goes through `write_atomic` (temp-then-rename), so an
// interrupted write leaves the prior valid file (§11.3.3). Source files are
// never touched (§11.3.4).

use std::path::{Path, PathBuf};
use std::{fs, io};

pub struct Sidecar {
    root: PathBuf,   // <project_root>/.tyo3
}

impl Sidecar {
    /// Bind to `<project_root>/.tyo3`. Does NOT create anything; a project with
    /// no sidecar is valid (§11.3.6). Creation is lazy, on first write.
    pub fn new(project_root: &Path) -> Self {
        Sidecar { root: project_root.join(".tyo3") }
    }

    pub fn exists(&self) -> bool { self.root.is_dir() }

    // ── Canonical paths (the ONLY place these are spelled) ──
    pub fn config_path(&self)        -> PathBuf { self.root.join("config.toml") }
    pub fn config_local_path(&self)  -> PathBuf { self.root.join("config.local.toml") }
    pub fn secrets_path(&self)       -> PathBuf { self.root.join("secrets.toml") }
    pub fn identity_db_path(&self)   -> PathBuf { self.root.join("identity.db") }
    pub fn authored_dir(&self, layer: &str) -> PathBuf { self.root.join("authored").join(layer) }
    pub fn cache_dir(&self, layer: &str)    -> PathBuf { self.root.join("cache").join(layer) }
    pub fn gitignore_path(&self)     -> PathBuf { self.root.join(".gitignore") }

    /// Ensure a directory under the sidecar exists (created lazily before a write).
    pub fn ensure_dir(&self, dir: &Path) -> io::Result<()> { fs::create_dir_all(dir) }

    /// Crash-safe write: write to `<path>.tmp`, fsync, rename over `path`
    /// (§11.3.3). The shared primitive every sidecar writer MUST use.
    pub fn write_atomic(&self, path: &Path, bytes: &[u8]) -> io::Result<()> {
        if let Some(parent) = path.parent() { fs::create_dir_all(parent)?; }
        let tmp = path.with_extension("tmp");
        { let mut f = fs::File::create(&tmp)?; use io::Write; f.write_all(bytes)?; f.sync_all()?; }
        fs::rename(&tmp, path)
    }
}
```
- **Refactor `IdentityRegistry` persistence into a clean format/I/O split.** Gate 2
  conflates serialization with durable I/O inside `save`/`load`. Separate the two
  properly: `IdentityRegistry` owns its *format* (`to_bytes() -> Vec<u8>` and
  `from_bytes(&[u8]) -> Result<Self, FormatVersionError>`, keeping the versioned
  JSON), and `Sidecar` owns *durable I/O and paths* (`write_atomic`, the
  `identity_db_path()`). The persist site becomes
  `sidecar.write_atomic(&sidecar.identity_db_path(), &registry.to_bytes())`, and load
  becomes `IdentityRegistry::from_bytes(&fs::read(...)?)`. Delete the now-redundant
  temp-then-rename and path construction from `identity.rs` and the three hardcoded
  `.tyo3/identity.db` sites in `project.rs`. This is the elegant separation the
  sidecar was introduced to enable; do it now while there is exactly one persisted
  format, so Gate 5–6 formats inherit the same clean split.
- Add `sidecar: Sidecar` to `HeadState` (constructed in `build_head` from `root`),
  so every write site already has it and no code below `HeadState` ever spells a
  sidecar path.

**Validate.**
- Unit test: `write_atomic` followed by a read returns the bytes; an interrupted
  write (write `.tmp`, do not rename) leaves any prior file at `path` intact.
- Unit test: `Sidecar::new` on a fresh dir reports `exists() == false` and creates
  nothing.
- **The on-disk format is unchanged**, so the Gate 2 round-trip and close→reopen
  identity tests must still pass. Any Gate 2 test that asserted the *internal* path
  construction (rather than load/save behaviour) is expected to move to the new
  `Sidecar`/`to_bytes` seam — update it deliberately.
- Grep gate: `rg '\.tyo3' rust/src/` shows occurrences **only** in `sidecar.rs`
  (plus comments). No other module spells the path.
- **Acceptance gate:** all pass; Gate 2 suite green. Commit.

---

## Step 2 — Typed config model + TOML parsing

**Goal.** A typed Rust model of `config.toml` that deserialises with documented
defaults, including the `config.local.toml` shallow overlay and `${VAR}` expansion
(SPEC §11.2; CONFIG_SCHEMA §1–2). No validation yet — that is Step 3.

**Files.** `rust/src/config.rs` (new); `mod config;` in `lib.rs`. Add `toml` to
`rust/Cargo.toml` (`serde` is already present).

**Build.**
```rust
use serde::Deserialize;
use std::collections::BTreeMap;   // BTreeMap → deterministic iteration order

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]     // unknown keys are a typed error, not silent (§11.3.5 spirit)
pub struct RawConfig {
    pub schema_version: u32,                                  // required
    #[serde(default)] pub project: ProjectCfg,
    #[serde(default)] pub spine: SpineCfg,
    #[serde(default)] pub hashing: HashingCfg,                // hashing.profiles.<name>
    #[serde(default)] pub layers: BTreeMap<String, LayerCfg>,
    #[serde(default)] pub generators: BTreeMap<String, GeneratorCfg>,
    #[serde(default)] pub stores: BTreeMap<String, StoreCfg>,
    #[serde(default)] pub coordination: CoordinationCfg,
    #[serde(default)] pub sidecar: SidecarCfg,
}
// … SpineCfg { retain_cap=256, default_hash_profile="structure" }
// … HashProfileCfg { whitespace_insensitive=true, normalize_trailing_commas=true,
//                    include_comments=false, include_docstrings=false }
// … LayerCfg { origin, depends_on=["code"], generator?, generator_version?,
//              hash_profile?, store?, serving="stale", recompute="lazy",
//              entity_kinds?, history=true, review_on_change=true, gc? }
// … GeneratorCfg, StoreCfg { backend, path?, url?, metric?, gc="never" }, etc.
```
- Use `#[serde(deny_unknown_fields)]` everywhere so a typo'd key is rejected, not
  ignored (consistent with "fail loudly", §11.3.5).
- **`Config::load(sidecar: &Sidecar) -> Result<RawConfig, ConfigError>`:**
  1. If `config_path()` is absent → return `RawConfig::defaults()` (see below).
  2. Else read it; if `config_local_path()` exists, parse both and **shallow-merge**
     local over committed at the table level (local keys win; whole sub-tables
     replace, per CONFIG_SCHEMA §Step-0 decision 5).
  3. Expand `${VAR}` in string values from the environment (and from
     `secrets.toml` if present), erroring on an undefined referenced var.
  4. Deserialize into `RawConfig`.
- **`RawConfig::defaults()`** synthesizes the minimal valid config: `schema_version`
  = current, one `structure` profile **whose fields equal Gate 2's `HashPolicy`
  defaults**, no extra layers. This is the no-config path and MUST match current
  behaviour (Step 4 verifies).

**Validate.**
- Unit test: the full annotated example from CONFIG_SCHEMA §1 deserialises without
  error (paste it into a fixture string).
- Unit test: a missing file yields `defaults()`; the default `structure` profile's
  four booleans equal Gate 2's `HashPolicy::default()` fields.
- Unit test: an unknown top-level key → deserialise error (proves `deny_unknown_fields`).
- Unit test: `config.local.toml` overriding `stores.vectors.path` produces the
  local value; committed config otherwise intact.
- Unit test: `${TYO3_TEST_VAR}` expands from the environment; an undefined
  `${MISSING}` errors.
- **Acceptance gate:** all pass. Commit. (Validation rules still pending — this step
  only parses.)

---

## Step 3 — Config validation (the correctness core)

**Goal.** Reject every malformed config with a precise typed error **before the
session opens** (CONFIG_SCHEMA §3; SPEC §9.2.2 static, §11.3.5). This is the
highest-value step in the gate — its test set is the deliverable.

**Files.** `rust/src/config.rs`.

**Build.** Implement `pub fn validate(raw: RawConfig) -> Result<ValidatedConfig, ConfigError>`,
running these checks in order (first failure wins, with a specific message):

```
1. schema_version is the supported version, else FormatVersion error (§11.3.5).
2. default_hash_profile names an existing [hashing.profiles.*].
3. Every layer's hash_profile (or the default) resolves to a defined profile.
4. Every derived layer's `generator` and `store` resolve to defined sections.
5. Layer names are unique and none is the reserved name `code`
   (the implicit layer 0 — §6 / CONFIG_SCHEMA §2.5).
6. depends_on graph over all layers + implicit `code` is ACYCLIC (§9.2.2 static).
   Build the edge set, run a cycle check (DFS/Kahn); a cycle → ConfigError naming
   the cycle. THIS IS THE STATIC DAG GATE.
7. No layer's depends_on names an `authored` layer (§9.2.4 — authored = sink).
8. origin discipline: derived layers define generator+generator_version+store and
   no authored-only keys; authored layers define none of the derived-only keys.
9. Every entity_kinds value is a known SymbolKind (Step-0 decision 6).
10. A vector store's bound generator `dim` (if both set) matches the store's
    expectation.
11. Secrets lint: no value looks like an inline credential (Step-0 decision 1).
```
- `ValidatedConfig` is `RawConfig` plus precomputed conveniences the later gates
  need: the **topologically-sorted layer order** (from check 6's graph — compute it
  once, here) and a `profile_for(layer) -> &HashProfileCfg` resolver. Storing the
  topo order here means Gate 5's DAG scheduler does not recompute it.
- Define `ConfigError` (Rust enum) with one variant per failure class
  (`UnknownVersion`, `DanglingRef`, `ReservedName`, `Cycle`, `OriginViolation`,
  `UnknownKind`, `DimMismatch`, `SecretInline`, `Parse`). Implement `Display`.

**Validate.** Pure-function unit tests (no engine, no session) — one per rule, each
asserting the **specific** error variant:
- Valid full example → `Ok`, and the topo order places `code` before `descriptions`
  before `description_embeddings`.
- `default_hash_profile = "nope"` → `DanglingRef`.
- A layer `depends_on = ["missing"]` → `DanglingRef`.
- Two layers forming a cycle (`a→b`, `b→a`) → `Cycle`. **(the §9.2.2 static gate)**
- A derived layer `depends_on` an authored layer → `OriginViolation` (or a dedicated
  variant) — §9.2.4.
- A layer named `code` → `ReservedName`.
- A derived layer missing `store` → `OriginViolation`.
- `entity_kinds = ["wizard"]` → `UnknownKind`.
- A value `api_key = "sk-livesecret…"` → `SecretInline`.
- **Determinism:** validating the same config twice yields byte-identical topo
  order (BTreeMap iteration + a total order on ties).
- **Acceptance gate:** every rule has a passing test asserting the right variant. Do
  not advance with any rule untested. Commit
  (`gate4: step 3 — config validation; static DAG acyclicity §9.2.2 closed`).

---

## Step 4 — Wire `HashPolicy` from the resolved profile

**Goal.** A layer's content hash is computed under its declared profile (SPEC
§7.2.2), and the **no-config default reproduces Gate 2 hashing exactly** (no
regression).

**Files.** `rust/src/hash.rs`, `rust/src/config.rs`.

**Build.**
- Add `impl From<&HashProfileCfg> for HashPolicy` (or a `HashProfileCfg::to_policy()`)
  mapping the four booleans onto Gate 2's `HashPolicy` fields one-to-one.
- Confirm `HashProfileCfg`'s defaults and `HashPolicy::default()` are field-for-field
  identical, so `defaults()` (Step 2) produces today's policy.
- Surface a `ValidatedConfig::hash_policy_for(layer: &str) -> HashPolicy` resolver
  (uses `profile_for`). The implicit `code` layer uses `default_hash_profile`.
- **Make config the single source of the hash policy now.** Gate 2 currently hashes
  entities under a hardcoded `HashPolicy::default()`. Replace that with the
  config-resolved policy for the `code`/identity layer
  (`cfg.hash_policy_for("code")`), threaded into `extract_entities`/reconciliation
  via `HeadState`. With the no-config default this is byte-identical to Gate 2, but
  it removes the hardcoded constant and means identity hashing already honours a
  project that customises its `structure` profile — the correct end-state, not a
  special case. *Per-layer derived* cache keys (a second policy per derived layer)
  are genuinely Gate 5 and stay out of scope; the resolver is the seam Gate 5
  consumes for those.

**Validate.**
- Unit test: `HashProfileCfg::default().to_policy() == HashPolicy::default()`.
- Unit test: a profile with `include_docstrings = true` produces a policy whose
  flag is set; hashing an entity under it differs from the default when only the
  docstring changed (reuse a Gate 2 hashing fixture).
- Regression: the Gate 2 hashing tests still pass unchanged.
- **Acceptance gate:** all pass. Commit.

---

## Step 5 — Load config on open; expose `session.config`

**Goal.** A session loads and validates config at open, fails open with a typed
error on a bad config, and exposes the validated config read-only to Python (SPEC
§11.3.5, §11.3.6). The spine `retain_cap` now comes from config.

**Files.** `rust/src/project.rs`, `rust/src/lib.rs`, `src/tyo3/config.py`,
`src/tyo3/session.py`.

**Build.**
- In `PyTyProject::open`:
  1. Build `Sidecar::new(&absolute)`.
  2. `let raw = Config::load(&sidecar)?;` then `let cfg = validate(raw)?;`
     mapping `ConfigError::UnknownVersion` → `FormatVersionError` and all other
     variants → `ConfigError` (Python). (Errors propagate as `PyErr`; the session
     does not open — §11.3.5.)
  3. Pass `cfg.spine.retain_cap` into the content store. Make the cap an explicit
     constructor parameter — `ContentStore::new(retain_cap)` — and update the Gate 1
     call sites accordingly; provide `ContentStore::default()` (= the 256 cap) for
     tests and the no-config path. An explicit parameter is cleaner than a hidden
     constant plus a second `with_…` constructor; the `retain_cap` is genuinely a
     configured policy, so the type should require it.
  4. Store `cfg` in `HeadState` so later gates read it under the write lock.
- Expose the validated config to Python across **one clean boundary**: a single
  `config_json(&self) -> String` returning the normalised, defaulted, validated
  config. Rust is the one validator and owns the canonical struct; Python receives an
  immutable, fully-typed mirror it constructs once at open. This is a deliberate
  single-serialization seam — not a pile of per-field PyO3 getters that would couple
  the two type trees and drift. (Serde already derives the JSON for free.)
- `src/tyo3/config.py`: a frozen dataclass tree (`TyConfig`, `LayerConfig`,
  `GeneratorConfig`, `StoreConfig`, …) with a `from_json(str)` constructor. Read-only.
- `session.py`: in `__init__`, after opening the native project, set
  `self._config = TyConfig.from_json(self._native().config_json())`; expose
  `@property config`. No exception translation needed — Step 8 makes the native
  `FormatVersionError`/`ConfigError` the canonical public types, so they propagate
  unchanged.

**Validate.** Python integration tests:
- Open a fixture **with no** `.tyo3/config.toml` → opens fine; `session.config`
  has the default `structure` profile and `retain_cap == 256`; behaviour matches
  Gate 3 (run a small existing read to confirm).
- Open a fixture **with** the full annotated `config.toml` → `session.config.layers`
  contains `embeddings`, `descriptions`, etc., in topological order.
- Open with `retain_cap = 4` → after 6 edits, `snapshot(at=evicted)` raises
  `RevisionEvictedError` (config actually drove the store).
- Open with a **cyclic** config → `ConfigError` raised, session not created.
- Open with `schema_version = 999` → `FormatVersionError` raised.
- **Acceptance gate:** all pass; `devenv shell -- tests` green. Commit.

---

## Step 6 — The store abstraction + `fs` backend + `cache/` scaffolding

**Goal.** Fix the artifact-store interface and the KV-vs-vector boundary once (SPEC
§8.2.6), ship the `fs` backend and the `cache/<layer>/` directory contract, and
make vector backends opt-in without the build depending on them. **No artifacts are
written yet** — that is Gate 5; this is the interface and scaffolding.

**Files.** `src/tyo3/stores/{__init__.py,base.py,fs.py}`, `src/tyo3/sidecar.py`.

**Build.**
- `base.py` — protocols keyed by **content hash** (the Gate-2/§8 key), never by
  revision or DurableId:
```python
class Store(Protocol):
    """Content-hash-keyed artifact store. Keys are (content_hash, generator_version)."""
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, artifact: bytes) -> None: ...
    def has(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...          # for the deferred orphan-GC

class VectorStore(Store, Protocol):
    """Adds nearest-neighbour search; ANN is delegated to the backend (§8.2.6)."""
    dim: int
    metric: str
    def nearest(self, query: Sequence[float], k: int) -> list[tuple[str, float]]: ...
```
- `fs.py` — `FsStore(Store)`: a content-hash-keyed file store under a directory
  (one file per key; key sharded into subdirs to avoid huge flat dirs). Uses the
  same temp-then-rename atomicity discipline as the sidecar.
- `__init__.py` — a backend **registry**: `open_store(store_cfg, sidecar) -> Store`.
  `backend = "fs"` → `FsStore(sidecar.cache_dir(layer))`. `"lancedb"`/`"qdrant"`/
  `"sqlite-vec"` → **lazy import** the adapter; if the dependency is not installed,
  raise a typed `StoreBackendUnavailable` (subclass of `TyO3Error`) naming the missing
  package. Do **not** import vector libs at module top level.
- `src/tyo3/sidecar.py` — Python-side helpers mirroring the Rust `Sidecar` paths
  (`cache_dir(layer)`, `authored_dir(layer)`), so Python writers (Gates 5–6) never
  hand-build paths either. Keep the two in sync; add a test asserting they agree on
  the layout for a sample layer name.

**Validate.**
- Unit test: `FsStore.put/get/has/delete` round-trips bytes; `get` of an absent key
  is `None`; an interrupted `put` leaves no half-file.
- Unit test: `open_store({"backend": "fs", ...})` returns an `FsStore` writing under
  `cache/<layer>/`; the directory is created lazily on first `put`.
- Unit test: `open_store({"backend": "lancedb"})` with the lib absent raises
  `StoreBackendUnavailable` naming `lancedb` (monkeypatch the import to simulate).
- Unit test: Python `sidecar.cache_dir("x")` and Rust's path agree (compare strings).
- **Acceptance gate:** all pass. Commit.

---

## Step 7 — `authored/` layout contract + sidecar policy (`gitignore_cache`)

**Goal.** Establish the on-disk contract for authored records and the cache-ignore
policy (SPEC §11.2, §11.3.1), so Gate 6 writes into a layout that already exists and
a team can commit `identity.db`+`authored/` while ignoring `cache/`. **No authored
records are written yet** — Gate 6 does that; here we fix the layout and the
`.gitignore`.

**Files.** `src/tyo3/sidecar.py`, `rust/src/sidecar.rs` (gitignore writer), `session.py`.

**Build.**
- Document and create-on-demand the **`authored/<layer>/` contract**: one record
  file per `DurableId` (e.g. `authored/<layer>/<durable_id>.json`), with an optional
  `authored/<layer>/<durable_id>.history/` when the layer's `history = true`. Add a
  `record_path(layer, durable_id)` / `history_dir(layer, durable_id)` helper to both
  the Rust and Python `Sidecar`. (Records themselves are Gate 6.)
- **`gitignore_cache` policy.** On open, if `sidecar.gitignore_cache` is true
  (default) and the sidecar exists, ensure `.tyo3/.gitignore` contains a `cache/`
  entry (and `config.local.toml`, `secrets.toml`). Write it crash-safely via
  `write_atomic` only if missing or stale — never clobber user additions; append the
  managed lines under a `# managed by tyo3` marker block. This makes `cache/`
  regenerable-and-ignorable while `identity.db`/`authored/` are committed (§11.3.1).
- Add a tiny `.tyo3/README` (or `LAYOUT.md`) marker on first sidecar creation
  describing the layout, so a collaborator who opens the directory understands it is
  inert tool data (§11.3.6).

**Validate.**
- Unit test: `record_path`/`history_dir` produce the documented paths; the `.history`
  dir is only used when the layer config has `history = true`.
- Integration test: open a project with `gitignore_cache = true` and a sidecar
  present → `.tyo3/.gitignore` lists `cache/`; a pre-existing user line in that file
  is preserved (managed block appended, not overwritten).
- Integration test (non-invasiveness, §11.3.6): opening a project creates/touches
  **nothing** outside `.tyo3/`; assert the source tree mtimes/contents are unchanged.
- **Acceptance gate:** all pass. Commit.

---

## Step 8 — Consolidated error model + format-versioning sweep

**Goal.** One typed error taxonomy for config and sidecar formats, surfaced to
Python, with every sidecar format versioned and rejecting newer versions loudly
(SPEC §13.1.1, §11.3.5). Finish the logging-facade discipline (§13.1.2).

**Files.** `rust/src/lib.rs`, `rust/src/config.rs`, `rust/src/identity.rs`,
`src/tyo3/exceptions.py`, `src/tyo3/session.py`.

**Build.**
- **Collapse the duplicated hierarchy into one taxonomy.** Today there are *two*
  parallel sets of exceptions: native `create_exception!` types in `lib.rs` and a
  separate pure-Python tree in `exceptions.py` (two distinct `ProjectClosedError`
  classes, etc.), bridged ad-hoc in `session.py`. That is exactly the kind of rough
  edge to fix here. Make the **native types canonical**: introduce a native
  `TyO3Error` base (via `create_exception!`) and reparent every TyO3 exception under
  it, then have `src/tyo3/exceptions.py` **re-export the native types** as the public
  `tyo3.exceptions` API instead of redefining them. Keep a single guarded fallback
  block for the native-not-built dev case (type-checking/import). After this there is
  one `ProjectClosedError`, one `TyO3Error` base, raised from Rust and caught in
  Python without a translation layer in `session.py`.
- Add two members to that one taxonomy (`create_exception!`, under `TyO3Error`):
  - `FormatVersionError` — an unknown/newer on-disk or config
    `schema_version`/`format_version`. **Unify** identity.db's existing
    `FormatError::UnknownVersion` to raise this, so *all* "newer format than I
    understand" cases are one catchable type.
  - `ConfigError` — config validation failures from Step 3.
- **Versioning sweep:** confirm `config.toml` (`schema_version`) and `identity.db`
  (`format_version`) both reject a newer version with `FormatVersionError`; document
  that `authored/` and `cache/` formats (Gates 5–6) MUST carry their own version and
  follow the same rule. Add a one-line "format version policy" note to CONFIG_SCHEMA.
- **Logging facade (§13.1.2):** grep for any `eprintln!`/`println!` introduced in
  this gate; replace with `log::` calls. Confirm `Config::load`/`validate` never
  write to stderr directly.

**Validate.**
- Test: `schema_version = 999` raises `FormatVersionError`, and
  `tyo3.exceptions.FormatVersionError is tyo3._native_impl.FormatVersionError`
  (one class, re-exported — proves the hierarchy was collapsed, not mirrored).
- Test: an `identity.db` with `format_version = 2` raises the *same*
  `FormatVersionError` as the config case (the unification).
- Test: `ProjectClosedError` raised from Rust is caught by
  `except tyo3.exceptions.TyO3Error` (single rooted taxonomy).
- Test: each Step-3 validation failure surfaces as `ConfigError` in Python with a
  message naming the offending key.
- Grep gate: no `eprintln!`/`println!` in `config.rs`/`sidecar.rs`.
- **Acceptance gate:** all pass; `devenv shell -- tests` green. Commit.

---

## Step 9 — Round-trip & non-invasiveness acceptance

**Goal.** Prove the gate's headline properties end-to-end in one dedicated module.

**Files.** `src/tyo3/tests/test_gate4_config_sidecar.py` (new) and a Rust
`#[cfg(test)]` module for the pure-Rust invariants.

**Build & validate** — a dedicated suite proving:
- **Minimal config opens** (no file → defaults; behaviour == Gate 3).
- **Full config validates** and exposes layers in topological order.
- **Each invalid config is rejected** with its specific typed error (cycle,
  dangling ref, reserved name, authored-as-dependency, unknown kind, bad version,
  inline secret).
- **Sidecar is the sole path owner** — `rg '\.tyo3' rust/src/ src/tyo3/` shows
  construction only in `sidecar.rs`/`sidecar.py` (the grep gate, as an asserted test
  or a CI check).
- **Crash-safe writes** — interrupted `write_atomic` leaves the prior valid file
  (config and identity.db).
- **Non-invasive** — opening touches nothing outside `.tyo3/`; a collaborator without
  TyO3 (delete `.tyo3/` entirely) opens with defaults and all Gate 1–3 tests pass.
- **`gitignore_cache`** writes the managed block and preserves user lines.
- **Acceptance gate:** all pass. Commit (`gate4: step 9 — config/sidecar acceptance`).

---

## Gate 4 — Final acceptance (must all pass before Gate 5)

Run `devenv shell -- tests` with the dedicated modules proving:

1. **Config single source of truth (§11.2).** Parsed + validated once in Rust;
   `session.config` exposes the complete, defaulted, topologically-ordered view.
2. **Static DAG acyclicity (§9.2.2 static).** A cyclic `depends_on` is rejected with
   `ConfigError(Cycle)` before the session opens; a valid graph yields a stored topo
   order.
3. **Origin discipline (§9.2.4).** Authored layers cannot be dependencies; derived
   layers require generator+version+store.
4. **Profiles drive hashing (§7.2.2).** Profile booleans map onto `HashPolicy`; the
   no-config default reproduces Gate 2 hashing exactly (regression-proven).
5. **Sidecar owns `.tyo3/` (§11.3.3/§11.3.4).** One path owner; crash-safe writes;
   source tree never touched.
6. **Format versioning (§11.3.5).** Newer `config.toml` or `identity.db` version →
   one unified `FormatVersionError`.
7. **Store interface fixed (§8.2.6).** `Store`/`VectorStore` protocols + `fs` backend
   + lazy vector backends + `cache/<layer>/` scaffolding; ANN delegated.
8. **Non-invasive (§11.3.6).** No config, or no sidecar at all, behaves exactly as
   Gate 3; deleting `.tyo3/` is safe.

When all eight pass on a clean `devenv shell -- tests`, tag the commit
`gate4-complete`. The declarative substrate and the sidecar are now final; layer
work (Gate 5) builds on them without re-cutting either.

## Carrying forward to Gate 5 (MUST read before derived layers)

Gate 4 deliberately leaves two **cut-once seams** that Gate 5 plugs into. They are
done here precisely so the derived-layer gate is additive:

- **The validated DAG of layers.** `ValidatedConfig` already holds the
  topologically-sorted layer order and the per-layer dependency edges (Step 3). The
  Gate-5 **runtime** derivation DAG (stale-marking, scheduled recompute) MUST consume
  this order rather than recomputing it, and MUST NOT re-validate acyclicity (already
  guaranteed). The static check lives here; the scheduler lives there.
- **The store interface and the cache directory.** Gate 5 writes artifacts via the
  `Store`/`VectorStore` protocols (Step 6) into `cache/<layer>/`, keyed by
  `(content_hash, generator_version)` (§8.2.1). It MUST NOT add a second storage path
  or key scheme — the boundary is fixed.
- **The in-lock invalidation hook.** Gate 5 inserts SPEC §3 step 5 ("mark derived
  layers stale") into the existing `commit_head` transaction, reading each layer's
  `hash_profile` via `ValidatedConfig::hash_policy_for` to compute the right cache
  key. Gate 4 added that resolver (Step 4) and stored the config in `HeadState`
  (Step 5) so the hook has everything it needs **inside the write lock** — no
  transaction restructuring required, only an insertion at the documented step.

Treat those three as the acceptance criteria carried into Gate 5, not background.

## Sequencing & escalation notes

- **Step 0 before everything.** The validator is only as correct as the locked
  schema. Do not let Step 3 drift from CONFIG_SCHEMA.
- **Refactor earlier-gate code where it makes the whole cleaner.** Steps 1, 4, 5,
  and 8 deliberately reshape Gate 1–2 code (format/I/O split, config-driven hash
  policy, explicit `retain_cap` constructor, one exception taxonomy). That is the
  point — the end-state architecture matters more than a small diff. The one
  invariant to hold is **no-config-equals-Gate-3** (§11.3.6): with no `config.toml`,
  observable behaviour is unchanged. Lean on the Gate 1–3 suites; if a test's
  *meaning* needs to change because the design improved, change it deliberately and
  call it out in the commit — don't contort the code to keep an old test literal.
- **Rust-vs-Python for config.** This guide parses+validates in **Rust** (single
  source of truth; feeds `HashPolicy` and `retain_cap`; unified format-version
  discipline with `identity.db`) and exposes a JSON view to Python. If the team
  instead validates in Python, the acyclicity check and `FormatVersionError`
  unification MUST still be enforced once and shared — do not split sidecar-format
  handling across two languages.
- **Do not pull in vector backends.** Step 6 ships only the `fs` backend; lancedb/
  qdrant adapters are lazy and out of scope until an embedding layer needs them
  (Gate 5). Adding them now would couple this foundation gate to heavy optional deps.
- **If `config.local.toml` merge semantics get complex**, keep it to a documented
  shallow per-table override; escalate rather than growing a deep-merge engine.
