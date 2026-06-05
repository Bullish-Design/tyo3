# TyO3 — `.tyo3/config.toml` Schema (Proposal)

> **Status: proposal for review.** Not yet normative. This drafts the schema for
> the sidecar configuration file referenced throughout `REFINED_ARCHITECTURE.md`
> (§6) and `REFINED_SPEC.md` (§11.2). It defines the spine settings, hashing
> profiles, layer definitions, generators, artifact stores, coordination, and the
> sidecar policy. Open questions are collected at the end.

`config.toml` lives at `.tyo3/config.toml`, is committed to version control, and is
the declarative description of the layers a project maintains and how they are
derived, hashed, stored, and served. It changes program behaviour only for
participants running TyO3 (SPEC §11.3.6).

---

## 1. Complete annotated example

```toml
# .tyo3/config.toml
schema_version = 1                 # config schema version; reader rejects unknown (SPEC §11.3.5)

[project]
name = "mylib"                     # optional; discovered if omitted

# ── Spine ────────────────────────────────────────────────────────────────────
[spine]
retain_cap = 256                   # retained-revision window for time-travel (SPEC §1.3.6)
default_hash_profile = "structure" # hashing profile used unless a layer overrides

# ── Hashing profiles ──────────────────────────────────────────────────────────
# Named normalisation policies that define what counts as a "meaningful" change
# for the entities a layer keys on (SPEC §7.2.2). Layers reference these by name.
[hashing.profiles.structure]
whitespace_insensitive = true
normalize_trailing_commas = true
include_comments = false
include_docstrings = false

[hashing.profiles.semantic]        # used by layers that care about prose/intent
whitespace_insensitive = true
normalize_trailing_commas = true
include_comments = true
include_docstrings = true

# ── Layers ─────────────────────────────────────────────────────────────────────
# Code (layer 0) is implicit and always present; it need not be declared. Declare
# additional derived and authored layers here.

[layers.embeddings]
origin            = "derived"
depends_on        = ["code"]       # DAG edge; must stay acyclic (SPEC §9.2.2)
generator         = "openai_embed" # → [generators.openai_embed]
generator_version = "text-embedding-3-large@v1"   # bump ⇒ logical invalidation (SPEC §8.2.4)
hash_profile      = "semantic"     # which content hash keys this layer's cache (SPEC §8.2.1)
store             = "vectors"      # → [stores.vectors]
serving           = "stale"        # "stale" | "block" (SPEC §8.2.5); default "stale"
recompute         = "lazy"         # "lazy" | "eager"; default "lazy"
entity_kinds      = ["function", "method", "class", "module"]   # optional filter

[layers.docstrings]
origin            = "derived"
depends_on        = ["code"]
generator         = "extract_docstring"
generator_version = "v1"
hash_profile      = "structure"
store             = "kv_docstrings"
serving           = "stale"

[layers.descriptions]
origin            = "derived"
depends_on        = ["code"]
generator         = "llm_describe"
generator_version = "claude@v1"
hash_profile      = "semantic"
store             = "kv_descriptions"
serving           = "stale"
recompute         = "eager"        # keep descriptions warm

[layers.description_embeddings]
origin            = "derived"
depends_on        = ["descriptions"]   # layered derivation: re-derives after descriptions
generator         = "openai_embed"
generator_version = "text-embedding-3-large@v1"
hash_profile      = "semantic"
store             = "vectors"

[layers.intent]
origin         = "authored"        # written deliberately; no upstream
history        = true              # keep prior versions of each record
review_on_change = true            # flag needs-review when the entity changes (SPEC §5.5.3)

# ── Generators ──────────────────────────────────────────────────────────────────
# How derived artifacts are produced. TyO3 orchestrates; it does not host models.
[generators.openai_embed]
type        = "http"
endpoint     = "https://api.example/embeddings"
model        = "text-embedding-3-large"
dim          = 3072
batch_size   = 128
concurrency  = 4
timeout_ms   = 30000

[generators.extract_docstring]
type     = "python"
callable = "mylib.tyo3_gen:extract_docstring"   # pure, in-process

[generators.llm_describe]
type        = "command"
command     = ["tyo3-describe", "--model", "claude"]
timeout_ms  = 60000
concurrency = 2

# ── Artifact stores ──────────────────────────────────────────────────────────────
# Where derived artifacts live. Keyed by (content_hash, generator_version). External
# vector search is delegated (SPEC §8.2.6).
[stores.vectors]
backend = "lancedb"                # "lancedb" | "qdrant" | "sqlite-vec" | "fs"
path    = "cache/embeddings"       # relative to .tyo3/
metric  = "cosine"                 # "cosine" | "l2" | "dot"

[stores.kv_docstrings]
backend = "fs"
path    = "cache/docstrings"

[stores.kv_descriptions]
backend = "fs"
path    = "cache/descriptions"

# ── Coordination ─────────────────────────────────────────────────────────────────
[coordination.bus]
queue_capacity = 1024              # per-subscriber bound (SPEC §12.2.4)
overflow       = "coalesce"        # "coalesce" | "block" | "error"; default "coalesce" (SPEC §12.2.2)

[coordination.watcher]
enabled     = false
debounce_ms = 200                  # collapse rapid disk events into one revision

# ── Sidecar policy ───────────────────────────────────────────────────────────────
[sidecar]
gitignore_cache = true             # cache/ is regenerable ⇒ may be ignored (SPEC §11.3.1)
```

---

## 2. Field reference

### 2.1 Top level
| Key | Type | Default | Notes |
|---|---|---|---|
| `schema_version` | int | — (required) | Reader **rejects** an unknown/newer version with a typed `FormatVersion` error (SPEC §11.3.5). |

### 2.2 `[project]`
| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | discovered | Informational. |

### 2.3 `[spine]`
| Key | Type | Default | Notes |
|---|---|---|---|
| `retain_cap` | int | 256 | Retained-revision window; older revisions evict and `snapshot(at=R)` raises `RevisionEvicted` (SPEC §1.3.6). |
| `default_hash_profile` | string | `"structure"` | Profile applied to layers that don't set `hash_profile`. Must name a profile in `[hashing.profiles.*]`. |

### 2.4 `[hashing.profiles.<name>]`
Defines what a "meaningful" change is for entities keyed under this profile
(SPEC §7.2). At least the `default_hash_profile` must exist.

| Key | Type | Default | Notes |
|---|---|---|---|
| `whitespace_insensitive` | bool | true | Cosmetic whitespace does not change the hash. |
| `normalize_trailing_commas` | bool | true | Trailing-comma style ignored. |
| `include_comments` | bool | false | Whether comments contribute to the hash. |
| `include_docstrings` | bool | false | Whether docstrings contribute. Semantic layers usually set true. |

> Two layers using the same profile share content-hash keys, so an unchanged entity
> can hit caches across layers that legitimately share normalisation. Layers with
> different sensitivity needs **must** use different profiles.

### 2.5 `[layers.<name>]`
`code` (layer 0) is implicit and need not be declared.

| Key | Applies to | Type | Default | Notes |
|---|---|---|---|---|
| `origin` | all | `"derived"` \| `"authored"` | — (required) | Fixes lifecycle. |
| `depends_on` | derived | string[] | `["code"]` | DAG edges; must be acyclic (SPEC §9.2.2). Authored layers **must not** appear as a dependency (SPEC §9.2.4). |
| `generator` | derived | string | — (required) | Names a `[generators.*]`. |
| `generator_version` | derived | string | — (required) | Part of the cache key; a bump logically invalidates the layer (SPEC §8.2.4). |
| `hash_profile` | derived | string | `default_hash_profile` | Which content hash keys this layer's cache (SPEC §8.2.1). |
| `store` | derived | string | — (required) | Names a `[stores.*]`. |
| `serving` | derived | `"stale"` \| `"block"` | `"stale"` | On staleness, serve last-good tagged `stale`, or block until recompute (SPEC §8.2.5). |
| `recompute` | derived | `"lazy"` \| `"eager"` | `"lazy"` | Recompute on first read, or eagerly on delta. |
| `entity_kinds` | derived | string[] | all | Restrict which symbol kinds this layer annotates. |
| `history` | authored | bool | true | Keep prior versions of each authored record. |
| `review_on_change` | authored | bool | true | Mark `needs-review` when the described entity changes (SPEC §5.5.3). |

### 2.6 `[generators.<name>]`
| Key | Type | Notes |
|---|---|---|
| `type` | `"python"` \| `"command"` \| `"http"` | Dispatch mechanism. |
| `callable` | string | `type="python"`: `"module:function"`, pure/in-process. |
| `command` | string[] | `type="command"`: argv; entity payload on stdin, artifact on stdout. |
| `endpoint` | string | `type="http"`: request URL. |
| `model` | string | Informational; document it in `generator_version` to make invalidation explicit. |
| `dim` | int | Embedding dimension (vector generators). Should match the bound store. |
| `batch_size` | int | Entities per generator call. |
| `concurrency` | int | Max concurrent generator calls. |
| `timeout_ms` | int | Per-call timeout; a timeout is a recompute failure (SPEC §9.2.6 — prior artifact retained). |

> Credentials are **not** stored here. Generators read secrets from the environment
> or a host-provided callback. `config.toml` is committed; it must contain no
> secrets (see Open Questions).

### 2.7 `[stores.<name>]`
| Key | Type | Notes |
|---|---|---|
| `backend` | `"lancedb"` \| `"qdrant"` \| `"sqlite-vec"` \| `"fs"` | Vector or KV backend. ANN is delegated (SPEC §8.2.6). |
| `path` | string | Local backends; relative to `.tyo3/`. |
| `url` | string | Remote backends (e.g. Qdrant). |
| `metric` | `"cosine"` \| `"l2"` \| `"dot"` | Vector backends. |

### 2.8 `[coordination.bus]`
| Key | Type | Default | Notes |
|---|---|---|---|
| `queue_capacity` | int | 1024 | Per-subscriber bound; a slow subscriber must not stall the writer (SPEC §12.2.4). |
| `overflow` | `"coalesce"` \| `"block"` \| `"error"` | `"coalesce"` | On overflow, coalesce with unioned affected set (SPEC §12.2.2), block the subscriber, or surface an error. |

### 2.9 `[coordination.watcher]`
| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | false | Run the file watcher as a change source. |
| `debounce_ms` | int | 200 | Collapse rapid disk events into one revision. |

### 2.10 `[sidecar]`
| Key | Type | Default | Notes |
|---|---|---|---|
| `gitignore_cache` | bool | true | `cache/` is regenerable; recommend ignoring it while committing `identity.db` and `authored/` (SPEC §11.3.1). |

---

## 3. Validation rules (load-time)

A config that violates any of these is rejected with a typed error before the
session opens:

1. `schema_version` present and supported, else `FormatVersion` (SPEC §11.3.5).
2. Every `hash_profile` / `store` / `generator` reference resolves to a defined
   section.
3. `default_hash_profile` and every referenced profile exist.
4. The `depends_on` graph over all layers (including implicit `code`) is **acyclic**
   (SPEC §9.2.2); cycle ⇒ reject.
5. No layer's `depends_on` names an `authored` layer (SPEC §9.2.4).
6. Derived layers define `generator`, `generator_version`, and `store`; authored
   layers define none of the derived-only keys.
7. A vector `generator.dim` (if set) matches its bound store's expectation.
8. No secrets appear in any value (best-effort lint; see Open Questions).
9. Layer names are unique and do not collide with the reserved name `code`.

---

## 4. Defaults and minimal config

The minimal valid file is just a version and one structure profile; the implicit
`code` layer works with no further declaration:

```toml
schema_version = 1
[hashing.profiles.structure]
```

Everything else (spine `retain_cap`, bus capacities, watcher off) takes documented
defaults.

---

## 5. Open questions (for review)

1. **Secrets for generators.** Confirmed they don't belong in committed config.
   Proposal: generators resolve credentials from environment variables named in a
   non-committed `.tyo3/secrets.toml` (gitignored) or via a host callback. Which?
2. **Per-layer `retain_cap` / store retention.** Should derived stores have their
   own GC policy (e.g. evict artifacts whose hash no entity references), separate
   from the spine's revision retention? Likely yes — propose a `[stores.<name>]
   gc = "orphans" | "never"` knob.
3. **`generator_version` ergonomics.** Should a model/prompt change auto-derive the
   version (hash of generator config) so users can't forget to bump it, instead of a
   manual string? Auto is safer but less legible in diffs.
4. **Profile inheritance.** Do we want `extends = "structure"` on profiles to avoid
   repetition, or keep profiles flat and explicit?
5. **Environment overlays.** Should a non-committed `config.local.toml` override
   committed config (e.g. point `stores.vectors` at a local path) for per-developer
   setup? Common need; adds merge complexity.
6. **Entity kinds vocabulary.** `entity_kinds` values should be pinned to a defined
   enum (function/method/class/module/variable/...) — needs a canonical list.
