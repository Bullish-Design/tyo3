# Option 2 — Syntactic Base-Class Resolution for INHERITS Edges

**Status:** concept / not implemented
**Author:** investigation follow-up to the graph-build performance work
**Prereq context:** Options 1 (supertypes-only Rust path) and 3 (warm-session reuse) are implemented. This document weighs the deeper, structural change.

---

## 1. The problem this targets

`CodeGraph.build()` is dominated by one thing: the type checker. Profiling tiny
fixtures (tens of lines) showed `type_hierarchy` costing **1–30 s per class**,
while every other pass is sub-second. The root cause is twofold:

1. **A discarded global scan.** `type_hierarchy` eagerly computes *subtypes*
   (a workspace-wide inheritor search over every module incl. typeshed). The
   graph only reads `.supertypes`. **Option 1 already removed this.**
2. **Cold type inference.** Even supertypes-only resolution calls
   `inferred_type` on the class and each base, which forces the salsa DB to
   build a large slice of the typeshed/stdlib inference world on first touch.
   Measured: first call ~28 s cold, then ~360 ms warm. **Option 3 amortizes
   this** by reusing one warm session across builds — but every *fresh* project
   (every new `TyO3Session`) still pays it once.

Options 1+3 shrink and amortize the tax. They do **not** eliminate it: a
first-ever build of a real project still pays full cold inference just to draw
`class Foo(Bar)` → `INHERITS` → `Bar`.

**Option 2 asks: do we need the type checker at all to know that `Foo`
inherits `Bar`?** For the overwhelming majority of Python, the base classes are
written right there in the source. If we read them syntactically, INHERITS
construction becomes inference-free — microseconds, no typeshed, no cold start.

---

## 2. Core concept

Replace the semantic `class_supertypes` call in `_resolve_inheritance` with a
two-step, inference-free pipeline:

1. **Syntactic extraction (no inference).** Parse the class definition and read
   `ClassDef.bases` — the literal base expressions: `Bar`, `pkg.Mixin`,
   `Generic[T]`, `Protocol`, etc. This is a pure AST walk over already-parsed
   source. ruff's parser (`parsed_module`) is already in the dependency tree and
   already used by the snapshot DB, so a new Rust `class_bases(path, line, col)`
   returning the base *expressions* (as text + range) costs microseconds and
   touches zero types. (A pure-Python `ast` parse is a fallback, but a Rust API
   keeps us on one parser and one source-of-truth for offsets.)

2. **Name resolution via existing graph machinery.** A syntactic base is a
   *name*, not a resolved class. To create an INHERITS edge we need a target
   `symbol_id` (`file::QualifiedName`). The graph **already** resolves names to
   targets for REFERENCES edges via `file_occurrences` + `_find_symbol_in_file`
   + `_infer_package`. Reuse exactly that:
   - Base name resolves to an in-project symbol → INHERITS edge to that node
     (nodes already exist; Pass 2 guarantees it).
   - Base name resolves to an external/unknown target → stub node keyed by
     inferred package (identical to how external references are handled today).
   - Base name unresolvable → skip (same failure semantics as a missed ref).

Conceptually: **INHERITS becomes a specialization of REFERENCE resolution**,
restricted to names appearing in a class's base list. No new resolution
philosophy — just a new source of name occurrences.

### What we lose access to

`ty_ide::type_hierarchy_supertypes` returns *resolved* `ClassLiteral`s with
their true defining file/range, and it injects implicit `object`. Going
syntactic means we resolve names ourselves and must reproduce these behaviors
(or consciously drop them). That is the whole tradeoff, detailed below.

---

## 3. Where it plugs in

Only `CodeGraph._resolve_inheritance` (graph.py) changes. The OVERRIDES
derivation that follows it is **unaffected in shape** — it walks the INHERITS
edges this step produces (`out_edges(... INHERITS ...)`) and collects ancestor
methods from the graph itself. As long as INHERITS edges land on the correct
in-project parent nodes, OVERRIDES keeps working verbatim.

`type_hierarchy` (the LSP-facing method) and Option 1's `class_supertypes`
remain available for callers that genuinely want semantic resolution or
subtypes. Option 2 is purely about which one *build* uses.

---

## 4. Detailed weighing

### 4.1 Pros

- **Eliminates the cold-inference tax for INHERITS outright.** No
  `inferred_type`, no `explicit_bases` (itself semantic), no typeshed touch.
  Parsing a class header is microseconds. Combined with Option 1 having already
  removed subtypes, inheritance resolution stops being a type-checker operation
  at all.
- **Build cost decouples from project + typeshed size.** Today cold cost scales
  with how much of typeshed a class's MRO drags in. Syntactic cost scales with
  source size only. On large real projects this is the difference between
  tens of seconds and tens of milliseconds for the inheritance pass.
- **Removes the last per-fresh-session cliff.** After Option 3, the remaining
  pain is the first build of any new project. Option 2 flattens it: a brand-new
  `TyO3Session`'s first graph build no longer needs the inference world just to
  draw inheritance edges. (`check()` still runs for diagnostics, ~1 s — see
  Opportunities for pushing that out of build too.)
- **Reuses trusted infrastructure.** Name→target resolution already exists and
  is exercised by every REFERENCES edge. We are not inventing resolution; we are
  feeding it one more class of inputs. Consistency with how the rest of the
  graph maps names to files is a feature.
- **A syntactic `class_bases` API is independently useful** — cheap structural
  queries, faster incremental updates, outline/structure features that never
  needed inference.

### 4.2 Cons

- **We re-implement a weaker resolver and own it forever.** ty's
  `explicit_bases`/`type_hierarchy_supertypes` resolve bases through the full
  semantic model: import aliases, re-exports, `TypeAlias`, conditional
  definitions, generic origins. Syntactic + `file_occurrences` covers the common
  cases but will diverge at the edges, and that divergence is now our
  maintenance burden, not ruff's.
- **Subscripted / computed / dynamic bases need explicit handling:**
  - `class X(Generic[T])`, `class X(Protocol[T])`, `class X(list[int])` — the
    base is a *subscript*; we must take the value (`Generic`, `list`) and
    decide whether it's edge-worthy. Easy to strip, easy to get subtly wrong
    (e.g. `Generic`/`Protocol` probably shouldn't produce a project edge).
  - `class X(metaclass=Meta)` — keyword, not a positional base; ty models the
    metaclass relationship, syntax must decide to ignore or special-case it.
  - Functional bases: `class X(namedtuple("X", ...))`, `class X(Enum_factory())`
    — no static name; ty may resolve, syntax cannot.
- **Import-alias resolution is on us.** `from a import B as C; class X(C)` — the
  base text is `C`; we must resolve `C` back to `a.B`. `file_occurrences`
  already resolves the *occurrence* of `C` in many cases, but star-imports,
  conditional `__all__` re-exports, and shadowing are where it gets thin.
- **No implicit `object` edge unless we add it.** ty injects `object` as the
  supertype of base-less classes; the current graph receives that edge. Syntax
  sees no base and would draw nothing. We must consciously decide to inject an
  `object` stub edge (to match today's output) or accept the behavior change.
- **OVERRIDES correctness rides on base resolution quality.** A base that
  resolves to the wrong node or to nothing degrades the INHERITS chain that
  OVERRIDES walks → missed or spurious override edges. This is the highest-risk
  correctness surface and needs targeted tests (multi-level inheritance,
  aliased bases, external bases).

### 4.3 Implications

- **Scope is larger than Option 1.** New syntactic extraction (Rust `class_bases`
  + DTO), a base-name resolver in Python that reconciles with existing
  reference resolution, and a careful reconciliation of output against the
  current semantic INHERITS/OVERRIDES edges. This is a *behavior change*: expect
  graph-diff churn that must be triaged fixture-by-fixture.
- **Acceptance bar = the existing parity tests.** `test_graph_incremental.py`
  already asserts `apply_delta` output is structurally equal to a full rebuild.
  Those equality checks become the regression net — but they assert equality to
  *the other syntactic build*, so they won't catch a divergence from today's
  *semantic* edges. We need an explicit before/after comparison against the
  current (Option 1) edge set on a representative corpus, with each diff
  classified as "acceptable" (e.g. dropped `Generic` edge) or "regression."
- **Two resolution paths to keep coherent.** REFERENCES and INHERITS would share
  resolution helpers; changes to `_find_symbol_in_file`/`_infer_package` now
  affect both. Net simpler conceptually, but the blast radius of edits to those
  helpers grows.
- **External-base fidelity drops to "name + package."** ty can tell you the real
  typeshed file for `Exception`; syntax gives `Exception` + inferred package
  `stdlib`. In practice the graph already collapses externals to package-keyed
  stubs, so this is mostly a non-loss — but anything wanting the resolved
  external definition must fall back to `type_hierarchy`/`resolve_external`.

### 4.4 Opportunities

- **Make `build` type-checker-free except for diagnostics.** With INHERITS
  syntactic and subtypes gone, the only remaining inference call in build is
  `check()` (diagnostics). That can be made *opt-in* — build a fast structural
  graph with zero inference, and attach diagnostics lazily or on demand. Build
  drops from tens of seconds to ~structural-parse time.
- **Inference becomes a feature-scoped concern, not a build-time tax.** Hover,
  goto-type, real type-hierarchy, and diagnostics pay for inference only when a
  user invokes them — the graph itself never forces the cold start.
- **Faster, cheaper incremental updates.** `apply_delta`'s re-index of changed
  files currently re-runs inheritance resolution (inference) per changed class.
  Syntactic resolution makes incremental inheritance updates trivially cheap,
  strengthening the MVCC incremental story.
- **Tunable fidelity.** Because resolution is now ours, we can choose policy:
  inject `object` or not, emit `Generic`/`Protocol` edges or not, mark
  syntactically-unresolved bases as `UNKNOWN` stubs for later semantic
  enrichment. A hybrid is possible: syntactic by default, with a per-class
  semantic fallback (`class_supertypes`) only when a base fails to resolve
  syntactically — paying inference for the rare hard case, not the common one.

---

## 5. Comparison to the implemented options

| Aspect | Opt 1 (supertypes-only) | Opt 3 (warm reuse) | **Opt 2 (syntactic)** |
|---|---|---|---|
| Removes discarded subtype scan | ✅ | — | ✅ (already gone) |
| Removes cold typeshed inference | ❌ (still infers bases) | ➖ (amortizes, 1×/session) | ✅ (no inference) |
| Effort / risk | tiny / very low | tiny / very low | **medium-high / semantic risk** |
| Behavior change | none | none | **yes — edges may differ** |
| Build cost scales with | typeshed slice | typeshed slice (once) | **source size only** |
| Ongoing maintenance | none | none | **owns base resolution** |

Opt 1+3 are the safe, done wins. Opt 2 is the structural ceiling-raiser with
real tradeoffs.

---

## 6. Recommended decision criteria

Pursue Option 2 **only if** at least one holds after Options 1+3 land:

1. First-build latency on **large real projects** is still unacceptable, and
   profiling confirms supertypes-only inference (not `check()`) is the
   dominant residual cost.
2. We want a build path that is **structurally pure** (zero inference) so
   inference can be made fully opt-in.
3. Incremental inheritance updates on big projects are a hot path.

If the residual cost turns out to be `check()` (diagnostics) rather than
supertypes inference, **Option 2 buys little** — the better lever is making
diagnostics lazy/opt-in. Measure the supertypes-only-vs-`check()` split first
(rebuild after Option 1 and re-profile) before committing.

---

## 7. Sketch of an implementation plan (if greenlit)

1. **Rust `class_bases(path, line, col) -> Vec<BaseRefDto>`** on `PySnapshot`:
   parse via `parsed_module`, locate the `ClassDef` at the position, return each
   base expression as `{ text, range, is_subscript, attribute_path }`. Pure
   syntax; no `db.check`, no `inferred_type`.
2. **Python `class_bases` wrapper** in `_ReadOps`, returning a small model.
3. **Resolver** in `_resolve_inheritance`: for each base, normalize subscripts,
   resolve the name via the existing reference machinery (reuse
   `_find_symbol_in_file`, `_infer_package`, and the same target-node
   ensuring logic), then `_add_edge(... INHERITS ...)`. Decide `object`
   injection policy explicitly.
4. **Parity harness:** build the same corpus with Option 1 (`class_supertypes`)
   and Option 2, diff INHERITS + OVERRIDES edge sets, classify every difference.
   Gate merge on "no unexplained regressions."
5. **Optional hybrid fallback:** if a base fails syntactic resolution, fall back
   to `class_supertypes` for that single class — bounded inference only for the
   hard cases.
6. **Re-profile** end-to-end build before/after on a large project to confirm
   the win is real and not masked by `check()`.

---

## 8. One-paragraph summary

Option 2 replaces semantic supertype resolution in graph build with a
syntactic read of `ClassDef.bases` plus the graph's existing name-resolution
machinery, removing type inference from the inheritance pass entirely. It is the
only option that structurally eliminates the cold-typeshed tax (Options 1 and 3
merely shrink and amortize it), and it decouples build cost from project +
typeshed size — but it trades the type checker's correctness for speed at the
edges (aliased/generic/dynamic bases, implicit `object`, external fidelity) and
makes us the owner of a base-resolution path that must be validated against the
current semantic output. Do it only if, after measuring with Options 1+3 in
place, supertypes inference (not `check()`) is still the dominant build cost or
we explicitly want an inference-free build.
