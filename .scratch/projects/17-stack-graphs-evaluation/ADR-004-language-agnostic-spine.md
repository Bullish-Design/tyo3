# ADR-004 — Making the tyo3 graph spine language-agnostic

- **Status:** Analysis / target design. **Outcome: a language-agnostic spine is
  achievable and is a *port-hardening* exercise, not a redesign — but it has one
  genuinely hard relocation (`hash.rs` cosmetic-stable normalization) and one
  subtle invariant (the MVCC weakest-snapshot contract) that a naive "abstract
  the type checker" pass would get wrong.** This ADR specifies the boundary, the
  language-neutral vocabulary the spine must own, the per-backend contract, and a
  phased path. No code change is proposed here.
- **Date:** 2026-06-10
- **Context project:** `17-stack-graphs-evaluation`
- **Builds on:** ADR-001 (stack-graphs is name resolution, not the core),
  ADR-003 (no generic type engine exists; depth = native engine per language),
  `SOURCE_ANALYSIS_SEAM_ADAPTER.md` (the `ReferenceResolver` seam). Where those
  asked "should a *specific* foreign engine be the core?", this asks the
  affirmative design question: **what does the spine need so that *any* language
  backend can sit below it?**

> One-line verdict: **the spine already speaks in `(name, kind, container, hash)`
> tuples and `id → id` edges — neutral data, not Python. Language-agnosticism is
> won by (1) turning the already-specified `SourceAnalysis` contract into a real
> `dyn` port, (2) relocating cosmetic-stable hashing from the spine into each
> backend, and (3) lifting three string/format conventions (entity-path,
> entity-kind, edge-kind) into a spine-owned neutral vocabulary. Everything
> language-specific lives below the port; the spine never names a language.**

---

## 1. Goal and scope

**Goal (hypothetical, user-stated):** run tyo3 against codebases in languages
other than Python — keeping the spine's value (durable identity, MVCC snapshots,
`affected` closure, derived/authored layers, the bus) while swapping the
semantic engine per language.

**In scope:** the contract between the spine and a per-language backend; the
neutral vocabulary; the coexistence model for a multi-language project.

**Out of scope (settled by ADR-003):** a *generic type engine*. There is no
"tree-sitter for types"; depth comes from a native engine per language
(`ty`/Python, rust-analyzer/Rust, `tsc`-or-LSP/TS, gopls/Go). This ADR assumes N
native backends, not one universal analyzer.

## 2. What "language-agnostic" actually decomposes into

Three independent axes, often conflated. Separating them is the whole game:

| Axis | Question | Where it lives |
|---|---|---|
| **A. Engine-agnostic** | spine depends on a *data contract*, not on `ty`/salsa | the `SourceAnalysis` port (§4) |
| **B. Vocabulary-neutral** | "entity kind", "container path", "edge kind", "cosmetic edit" defined without Python assumptions | spine-owned neutral types (§5–6) |
| **C. Multi-language coexistence** | one project spanning N languages; cross-language edges; per-file backend dispatch | project/session layer (§8) |

Axis A is ~70% done (DTOs are already neutral, V2 split producer from engine).
Axis B is where the real Python assumptions hide (`hash.rs`, the `file::a::b`
path convention). Axis C is new surface but conceptually straightforward once A+B
hold.

## 3. Coupling inventory (grounded in the code)

Green = already neutral. Yellow = neutral data, Python-coupled *production*.
Red = genuinely Python-specific logic that must be relocated below the port.

| Component | File | State | Note |
|---|---|---|---|
| `SymbolDto` / `NameOccurrenceDto` / `CodeDeltaDto` | `rust/src/dto/` | 🟢 | Plain data: name, kind, range, container_name, durable_id, content_hash(es). No `ty` types. |
| Identity reconcile (`mint`/`reconcile`, PATH/HASH/STRUCT passes) | `identity.rs:35,612` | 🟢→🟡 | Keys on `(name, kind, container)` + content hash — neutral. **Soft coupling:** the path convention `a.py::Outer::helper` bakes in file extension + `::` nesting (§5). |
| `reverse_deps` / `affected_closure(_with_deleted)` | `code_layer.rs` | 🟢 | Pure graph over ids/edges. Language-blind already. |
| Revisions / snapshots / B+ commit | `overlay.rs`, `content.rs` | 🟢 | MVCC over opaque content. Neutral — but see §7 invariant. |
| Derived / authored layers, bus | `authored.rs`, coordination | 🟢 | Keyed by id + ContentHash. Neutral. |
| `Builder` producer threads `&TyProjectState`; calls `ty_ide::*` | `code_layer.rs:435,466,490,1125` | 🟡 | **The main engine coupling.** Neutral output, Python-only production. → behind the port (§4). |
| **Cosmetic-stable content hashing** | `hash.rs:49,136,149` | 🔴 | Uses `ruff_python_ast` + `ruff_python_parser` source-order AST visitor and a Python-specific `HashPolicy` (`normalize_trailing_commas`, …). **This is the deepest language assumption in the whole spine.** → backend-supplied (§6). |
| `SymbolKindDto` | `dto/symbols.rs` | 🟢 | Modelled on LSP `SymbolKind` — already a cross-language enum. |
| Edge kinds (References/Imports/Inherits/Overrides/Defines/Contains) | producer | 🟡 | Concepts are cross-language; the *set* may need a neutral, extensible enum (§5). |
| Config | `config.rs` | 🟡 | No first-class `language` concept; generator `type="python"` is a *layer* generator, unrelated to the engine. Needs a per-path backend selector (§8). |

**The headline:** the spine logic is already neutral. The two real Python
residues are (1) the producer's `ty_ide` calls (easy — a `dyn` boundary) and (2)
`hash.rs` normalization (hard — per-language AST work). Get those two below the
port and Axis A+B are essentially won.

## 4. The port: `SourceAnalysis` as a real `dyn` boundary

The spine spec (`tyo3-spine.allium`) *already names* this contract —
`SourceAnalysis.symbols_at` / `.apply_change` / `.supertypes` — it is simply
realised concretely by `ty` today rather than as a swappable trait. The work is
to make the named contract a real boundary the producer depends on:

```rust
/// Everything the spine needs from a language engine. The producer depends on
/// `&dyn SourceAnalysis`; no spine module names `ty`, `salsa`, or a language.
trait SourceAnalysis {
    /// Structural entities in a file, in neutral form: name, kind, container
    /// path, range, and a backend-computed cosmetic-stable content hash (§6).
    /// NO durable_id — identity is the spine's, minted from these (§5).
    fn symbols(&self, snap: &BackendSnapshot, file: &FileId) -> Vec<NeutralSymbol>;

    /// Resolved reference/import occurrences in DurableId-free, target-path form;
    /// the spine maps target paths → DurableId via its own identity index.
    /// Backends that can do type-directed resolution emit inference-flow edges
    /// here; syntactic backends emit only nominal ones (capability, §7).
    fn edges(&self, snap: &BackendSnapshot, file: &FileId) -> Vec<NeutralEdge>;

    /// Supertypes for inheritance/override edges (may be empty for languages
    /// without nominal inheritance).
    fn supertypes(&self, snap: &BackendSnapshot, entity: &EntityPath) -> Vec<EntityPath>;

    /// Backend-owned cosmetic-stable hash (§6). The spine treats the result as an
    /// opaque token; only the backend knows what is "cosmetic" in this language.
    fn content_hash(&self, snap: &BackendSnapshot, entity: &EntityPath, profile: HashProfile) -> ContentHash;

    /// Static description of what this backend can do (§7).
    fn capabilities(&self) -> Capabilities;
}
```

Everything *above* the port — node upsert, identity minting/reconcile, hashing
*orchestration*, `reverse_deps`, `affected`, commit, layers, bus — is unchanged
and language-blind. Everything `ty`-specific (`TyProjectState`, `ty_ide`, the
salsa db lifetime) sits *below* it, in a `PythonBackend: SourceAnalysis` impl.

`BackendSnapshot` is the abstraction over "an immutable view of revision R" —
see §7 for why its contract must be the *weakest* common denominator.

## 5. The neutral vocabulary the spine must own

Three string/format conventions are currently implicit and Python-flavoured.
They must become explicit, spine-owned, backend-fed types:

1. **EntityPath / container.** Today identity uses `a.py::Outer::helper`
   (`identity.rs:1112`) — file + `::`-nested containers. This *mostly* generalises
   (Rust `mod::Type::method`, TS `file::Namespace::Class::method`), but the spine
   must stop assuming a `.py` extension or `::` literal and instead consume a
   **structured path**: `EntityPath { file: FileId, container: Vec<Segment>,
   name: Segment }`. Reconciliation's `(name, kind, container)` STRUCT pass
   (`identity.rs:726`) then works unchanged over structured segments. The backend
   supplies the segmentation (it knows the language's nesting rules).

2. **EntityKind.** `SymbolKindDto` already mirrors LSP `SymbolKind` (Class,
   Method, Function, Field, …) — cross-language. Keep it as the neutral kind set;
   let backends map their native constructs onto it. Languages with constructs
   that don't fit (Rust `trait`, Go `interface`) map to the nearest LSP kind plus
   an optional backend-specific tag the spine treats opaquely.

3. **EdgeKind.** References/Imports/Inherits/Overrides/Defines/Contains are
   cross-language *concepts*, but the set should be a **closed neutral enum with
   an `Other(tag)` escape**, so a backend can emit e.g. `Implements` (Go/Rust
   interfaces) without a spine change. `reverse_deps`/`affected` don't care about
   the kind — they walk any edge — so new kinds are free to the closure.

The rule: **the spine owns the *shape* of these types; backends own the *mapping*
from their language into them.**

## 6. The hard relocation: cosmetic-stable hashing

`hash.rs` is the single deepest language assumption. The durable-identity story
depends on a `ContentHash` that is **stable under cosmetic edits** (reformatting,
blank lines, trailing commas) and **changes on meaningful edits** (rename, literal
change) — see its own tests (`hash.rs:398-429`). Today it achieves that by
parsing with `ruff_python_parser` and walking a `ruff_python_ast` source-order
visitor under a Python `HashPolicy`. **None of that generalises.**

Relocation:

- **Below the port:** each backend implements `content_hash(...)`. "Cosmetic" is
  inherently per-language — only the Rust backend knows Rust formatting rules,
  only the TS backend knows TS's. Each backend brings its own AST normaliser
  (typically the same parser its engine already uses — free, like `ty`/ruff
  shares the parse).
- **In the spine:** `ContentHash` becomes a truly **opaque token**. The spine
  already treats it opaquely for keying derived caches and the HASH reconcile
  pass (`identity.rs:485`); it must stop *computing* it and only *consume* it.
- **The contract the spine requires of any hasher** (the language-neutral
  invariant that makes identity work, lifted out of the Python impl):
  1. *Determinism* — same entity bytes ⇒ same hash, across runs.
  2. *Cosmetic-stability* — edits the language considers non-semantic ⇒ same hash.
  3. *Sensitivity* — a rename or value change ⇒ different hash.
  4. *Locality* — an entity's hash depends only on that entity's normalised form,
     not its neighbours (so `affected` stays scoped).
  These four are the *real* spec; the Python `HashPolicy` is one implementation
  of them. The spine tests these as a **backend conformance suite** (§9), not as
  Python-specific assertions.

This is the one place where "just add a language" is genuinely non-trivial: a new
backend isn't done until it passes the four-property hashing conformance suite.
A purely syntactic (tree-sitter-only) backend *can* satisfy 1/3/4 trivially but
will be weaker on 2 (it sees formatting it can't prove is cosmetic) — an honest,
documented degradation, not a blocker.

## 7. Capability model + the MVCC weakest-contract invariant

**Capabilities.** Backends differ in power; the spine must degrade, not break:

```rust
struct Capabilities {
    type_directed_edges: bool,   // inference-flow (w = make(); w.draw()) vs nominal-only
    supertypes: bool,            // nominal inheritance exists in this language
    cosmetic_stable_hash: bool,  // full property-2 hashing, or syntactic-approx only
    shared_snapshots: bool,      // see invariant below
}
```

`affected` precision already degrades to container-granular when type-directed
edges are absent — exactly the V2 async-refinement shape. A syntactic backend
reports `type_directed_edges: false` and the spine asks for nothing it can't give.

**The subtle invariant (do not get this wrong).** The MVCC commit protocol has a
hidden dependency on the *backend's* concurrency model. tyo3's Strategy B+
deferred-publish exists **because salsa cannot be shared with readers or rolled
back** — ADR-002 §3 calls this "the load-bearing law." A different backend might
permit true Strategy A (snapshot + rollback), or impose its own constraint.

> **Rule:** design the commit protocol to the *weakest* backend snapshot
> contract — "give me an immutable view of revision R; let me apply overlay edits
> without mutating prior views" — and assume nothing stronger. B+ already assumes
> exactly this, so the current protocol is safe over *all* backends. The danger
> is the temptation, once a stronger backend appears, to special-case a faster
> commit path. Resist it: capable backends may be faster *below* the port (their
> own snapshot impl), never via a different spine commit path.

This is the invariant a naive "abstract the type checker" refactor would miss,
because it lives in the commit protocol, not in the obvious analysis seam.

## 8. Multi-language coexistence (Axis C)

Once A+B hold, a project spanning N languages needs three additions:

1. **Per-file backend dispatch.** A `language_of(FileId) -> BackendId` map
   (config: glob/extension → backend), so the producer routes each file to the
   right `SourceAnalysis` impl. Config gains a first-class `[languages]` table
   (today there is none — `config.rs` only has Python *layer* generators).

2. **One identity space, N backends.** Identity, revisions, `affected`, layers,
   and the bus stay **single, shared, language-blind** — a `DurableId` for a
   Python class and one for a TS class are the same kind of token. This is the
   payoff: the durable-identity / MVCC / derived-layer machinery is written once
   and amortised across every language.

3. **Cross-language edges (optional, advanced).** A Python FFI call into a Rust
   module, a TS import of a generated client — these are `id → id` edges whose
   *source* and *target* come from different backends. The spine can represent
   them natively (it's just an edge); *producing* them needs a resolver that
   spans backends (e.g. a binding-registry). Mark as a **future capability**, not
   a v1 requirement — most value lands with per-language graphs in one identity
   space, no cross-language edges.

## 9. Reference backends (sketch, to pressure-test the port)

| Backend | Engine | type_directed | hash property-2 | Notes |
|---|---|---|---|---|
| **Python** | `ty` (existing) | ✅ | ✅ (ruff AST) | The reference impl; refactor `Builder` to sit behind the port. |
| **Rust** | rust-analyzer (lib) | ✅ | ✅ (ra parser) | Richest non-Python; ra is salsa-based too → same weakest-contract assumptions hold for free. |
| **TypeScript** | `tsc` API or tsserver/LSP | ✅ | ✅ (ts AST) | LSP-wrapping path: occurrences/defs via LSP, hashing via a TS parser. |
| **Go** | gopls (LSP) | ✅ | ⚠️ (gofmt-normalise) | Interfaces → `EdgeKind::Other("Implements")`. |
| **Any (floor)** | tree-sitter + stack-graphs fork | ❌ (nominal only) | ⚠️ syntactic-approx | The ADR-001 §7 multi-language floor: cheap nav-grade edges, coarse `affected`, honest degradation. Useful for long-tail languages with no native engine integration. |

The port survives all five, which is the test of whether it's actually neutral.
Note the **two integration styles**: *in-process library* (ty, rust-analyzer —
fast, shares parse) vs *LSP-wrapping* (gopls, tsserver — slower, process boundary,
but enormous language reach for little code). The port should not assume in-process;
`BackendSnapshot` may wrap an out-of-process session.

## 10. Phased path (if the goal is ever pursued)

1. **P1 — Name the port.** Extract `trait SourceAnalysis` + `Capabilities`;
   make `Builder` depend on `&dyn SourceAnalysis`; wrap today's `ty` calls as
   `PythonBackend`. Pure refactor, behaviour-identical, single language. *This is
   the 80%-value step and is worth doing on its own merits (testability, seam
   clarity) even if no second language ever lands.*
2. **P2 — Relocate hashing.** Move cosmetic-stable hashing into `PythonBackend`;
   make `ContentHash` opaque to the spine; codify the four-property **hashing
   conformance suite** as the backend contract test.
3. **P3 — Neutralise vocabulary.** Replace stringly `file::a::b` paths with
   structured `EntityPath`; make `EdgeKind` a closed enum + `Other(tag)`; confirm
   reconcile/`affected` unaffected.
4. **P4 — Per-file dispatch + config.** Add `[languages]` config and
   `language_of(file)` routing; still one backend in practice.
5. **P5 — Second backend.** Integrate one real non-Python backend
   (rust-analyzer is the cleanest fit — same salsa model). Prove the identity /
   MVCC / layers machinery is genuinely shared.
6. **P6 — (optional) cross-language edges.** Only if a concrete need appears.

P1–P3 are the real work and are *independently valuable hardening* of the
existing Python system. P4+ are only paid for when a second language is actually
wanted.

## 11. Risks / open questions

- **Hashing is the gate.** Property-2 (cosmetic-stability) is genuinely
  per-language and is what makes durable identity *durable*. A backend that gets
  it wrong silently corrupts identity (spurious re-mints, broken layer carryover).
  The conformance suite is non-negotiable, and syntactic backends are honestly
  weaker here.
- **LSP-wrapping latency vs. eager `affected`.** Out-of-process backends add
  per-call latency; the eager in-commit `affected` closure may need batching or
  an async-refinement path (the V2 channel already exists) for LSP backends.
- **Identity across heterogeneous path conventions.** Structured `EntityPath`
  must be expressive enough for every target language's nesting (Rust modules,
  TS namespaces/declaration-merging, Go packages). Risk of an over-fit shape;
  design against ≥3 languages on paper before committing.
- **Don't reopen ADR-003.** The temptation under "multi-language" is to reach for
  a generic resolver/type engine. The answer remains: native engine per language
  *below the port*. Language-agnosticism is a *boundary* property, not a *shared
  engine* property.

## 12. Decision

- **Adopt** the framing: language-agnosticism = a hardened `SourceAnalysis` port
  + opaque `ContentHash` + neutral `EntityPath`/`EdgeKind` vocabulary + capability
  flags, with all language specifics below the port and **one shared spine**.
- **Recommend P1–P3 regardless of multi-language**, as they harden and clarify
  the existing single-language system (real seam, conformance-tested hashing,
  structured paths) at low risk.
- **Gate P4+** on a concrete second-language need; rust-analyzer is the
  recommended first non-Python backend (shared salsa model → the §7 invariant
  holds for free).
- **Hold the §7 weakest-snapshot invariant** as a hard rule in the commit
  protocol; never special-case a stronger backend's commit.
- **Reaffirm ADR-003:** no generic type engine; depth is per-language native
  engines behind the port.

## 13. References

- `ADR-001-stack-graphs-as-core.md` §7 (the multi-language condition).
- `ADR-002-incremental-spine.md` §3 (the MVCC "load-bearing law").
- `ADR-003-statix-type-layer.md` (no generic type engine; native engine per language).
- `SOURCE_ANALYSIS_SEAM_ADAPTER.md` (the `ReferenceResolver` seam, subset of this port).
- Code: `rust/src/code_layer.rs` (producer, `TyProjectState`, `ty_ide` calls),
  `rust/src/identity.rs` (reconcile passes, `(name,kind,container)` keying),
  `rust/src/hash.rs` (Python-AST cosmetic-stable hashing — the relocation target),
  `rust/src/dto/symbols.rs` (`SymbolDto`, `SymbolKindDto`), `rust/src/config.rs`
  (no first-class language concept yet).
- Spec: `.scratch/specs/tyo3-spine.allium` (`SourceAnalysis` contract, already named).
