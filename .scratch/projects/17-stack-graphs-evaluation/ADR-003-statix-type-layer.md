# ADR-003 — A scope-graph / Statix type layer as tyo3's resolution substrate

- **Status:** Rejected (as a *generic type engine*); the lineage's *name-binding*
  half is already covered by ADR-001's conditional resolver seam. This ADR
  records the branch ADR-001 stopped short of: the **type-checking** half of the
  scope-graph family (Statix / Spoofax), what adopting it would take, and why
  GitHub deliberately did *not* take it.
- **Date:** 2026-06-10
- **Context project:** `17-stack-graphs-evaluation`
- **Builds on:** ADR-001 (stack-graphs is name resolution, not the core) and
  `SOURCE_ANALYSIS_SEAM_ADAPTER.md` (the one seam where a syntactic resolver
  fits). ADR-001 §5.2 / §7 flagged "a real type checker *can* do better here;
  stack-graphs cannot, ever" but did not analyse the type-capable ancestor of
  stack-graphs. This ADR closes that gap.

> One-line verdict: **the scope-graph lineage *does* reach types — via Statix's
> constraint solver + unification — but stack-graphs threw that half away on
> purpose to buy per-file incrementality. Re-introducing it is "build a type
> checker per language in a research meta-DSL," which is redundant against `ty`
> for Python and worse-than-native everywhere else. There is no production
> "tree-sitter for types," and the reasons it doesn't exist are the same reasons
> GitHub shipped only the name-resolution half.**

---

## 1. The question

ADR-001 evaluated [stack-graphs](https://github.com/github/stack-graphs) and
found it purely syntactic — no type inference, by design. The natural follow-up:
its academic ancestor, **scope graphs**, *was* built to do type checking
(Néron/Tolmach/Visser/Wachsmuth, ESOP 2015 — ref [18] in ADR-002), and its
modern embodiment **Statix** (Spoofax) does name binding *and* static typing in
one framework. So:

> Could a scope-graph / Statix-style type layer be a **language-agnostic
> resolution + type substrate** for tyo3 — a "tree-sitter for types" — replacing
> or generalising the per-language `ty` dependency?

As with ADR-001, fork/maintenance cost is noted but the decision rests on
architectural fit.

## 2. The lineage, stated precisely

```
A Theory of Name Resolution (scope graphs)      ESOP 2015   ── name binding, formal
        │
        ├──► Statix / NaBL2 (Spoofax)            OOPSLA 2018 ── name binding + TYPE CHECKING
        │      "Scopes as Types": scopes model           via constraint generation + a
        │      record/generic type structure;            unification-based constraint solver
        │      resolution queries are type-directed       (solver state = constraints + unifier
        │                                                  + scope graph, solved to fixpoint)
        │
        └──► stack-graphs (GitHub)               2021       ── name binding ONLY, + incrementality
               kept the resolution half;                     each file → partial paths in ISOLATION,
               DROPPED the solver/types;                     stitched at query time. No solver.
               weakened the type-directed
               "scope stack" into a type-erased shadow
```

The single load-bearing fact: **type-directed resolution and incremental,
per-file isolation are in tension, and stack-graphs resolved that tension by
deleting the types.** Statix keeps the types and pays with a whole-module
constraint solve; stack-graphs keeps per-file isolation and pays by being
syntactic. You cannot have both cheaply — this is the crux of the whole
evaluation.

## 3. What Statix actually is (grounding the "type" claim)

- **Constraint generation.** Traversing the AST emits *constraints*: equality
  (unification) constraints over type terms, and *scope-graph constraints*
  (declare scope, declare edge, resolve query) for name binding.
- **A solver with a unifier.** The solver state is a triple — outstanding
  constraints, a unifier, and the scope graph being built. It runs to a fixpoint;
  the residual unifier + scope graph *is* the typing + binding solution.
- **Scopes as types.** The internal structure of types (record fields, generic
  parameters, class members) is modelled with the *same* scope-graph machinery,
  so attribute lookups (`x.foo`) are scope-graph queries that depend on `x`'s
  inferred type — i.e. resolution is type-directed.
- **Status.** Research tooling inside the **Spoofax** language workbench. There
  is no standalone, production-grade, multi-language Statix library you can
  depend on the way you depend on tree-sitter. (Contrast: tree-sitter and
  stack-graphs both shipped as embeddable libraries.)

## 4. What adopting it would take for tyo3

Stack-graphs cannot be *extended* to types — the solver was removed. To get
Statix-grade typing you must **re-introduce what GitHub deleted**:

1. A **constraint-generation pass per language** (the static-semantics spec).
2. A **unification-based constraint solver** (terms + type-directed scope-graph
   queries with the partial-information handling that makes attribute lookup
   work).
3. A **type-system specification per language**, authored in a Statix-like
   meta-DSL.

That is the definition of *building a type checker*. Concretely:

- **For Python:** `ty` already is that type checker — hand-written, far more
  complete than any constraint-DSL re-spec, and already wired into the salsa
  incremental db tyo3 shares (`rust/Cargo.toml:38-56`). A Statix Python spec is
  strictly redundant and strictly behind.
- **For other languages:** you'd author a type system per language in a research
  meta-language, landing *behind* the native checker (rust-analyzer / `tsc` /
  gopls) you could instead wrap. Multi-person-year, research-grade effort per
  language, for a worse result than the off-the-shelf engine.

So the "language-agnostic type substrate" collapses to either "redundant with
`ty`" (Python) or "reinvent, worse, the wheel you could wrap" (everything else).

## 5. Why GitHub did NOT take the Statix/full-type path

Four reasons, and each is the mirror image of a tyo3 choice — which is exactly
why stack-graphs is a poor donor for *our* spine and an excellent one for
*GitHub's* indexer:

1. **Type inference resists per-file isolation; name resolution mostly tolerates
   it.** Stack-graphs analyses each file alone at index time and stitches at
   query time — the property that makes it cheap enough to run on every commit to
   every public *and* private repo. A Statix solver needs whole-module (often
   whole-program) information; adopting it destroys that isolation.

2. **Zero-config, build-free, works-on-broken-code.** A type checker needs the
   build environment, dependency versions, resolved imports — none of which
   GitHub has at index time, and it explicitly wanted nav that works on
   half-written, non-compiling code with no per-repo setup. Type checking
   structurally cannot deliver that.

3. **Product goal is navigation, where over-approximation is acceptable.**
   Returning several candidate definitions is fine for go-to-def; name-binding
   rules per language are tractable. A *type system* per language is enormous and
   buys precision the navigation product doesn't need.

4. **Engineering maturity / scale.** Statix/Spoofax was not built for
   GitHub-scale throughput. Stack-graphs was a deliberate productionisation of
   *only the tractable half* of the academic work.

GitHub traded **precision** for **scale + incrementality + language-agnosticism**.
tyo3 made the **opposite** trade: precision (a real type checker, `ty`) for one
language. Both are correct for their goal; they are not interchangeable.

## 6. The general law (why "tree-sitter for types" doesn't exist)

Three things generalised cleanly into portable, embeddable engines:

- **Parsing** → tree-sitter (grammar + engine).
- **Name binding** → stack-graphs (rules + one resolution algorithm) — *almost*
  as clean, now archived (2025-09-09).
- **Type inference** → *did not generalise.* Type systems differ enough between
  languages that the portable-DSL approach exists only at the research frontier
  (scope graphs / Statix / Spoofax), and no one has shipped it as a maintained,
  multi-language library.

This is precisely *why* `ty`, pyright, rust-analyzer, and gopls are hand-written
per-language semantic engines rather than rule-sets over a shared type engine.
The non-existence is structural, not a market gap.

## 7. Decision

- **Reject** a scope-graph / Statix type layer as tyo3's resolution/type
  substrate. It is redundant against `ty` for Python and a from-scratch
  type-checker build for any other language.
- **Reaffirm ADR-001:** the only conditional opening is the *name-binding* half
  (a stack-graphs fork behind the `ReferenceResolver` seam), gated on the
  multi-language goal in ADR-001 §7 — and now carrying full fork-maintenance cost
  since upstream is archived.
- **Affirm the architecture:** depth comes from a **native semantic engine per
  language behind the spine** (`ty` for Python; rust-analyzer / `tsc` / gopls
  equivalents elsewhere), never from a generic type engine.

## 8. Consequences

- **Positive:** the "make tyo3 multi-language" instinct is correctly aimed at a
  *backend boundary*, not at a generic type formalism that doesn't exist in
  production. No multi-year research detour.
- **Negative / accepted:** tyo3 stays tied to one semantic engine per language;
  multi-language reach means integrating N native engines, each behind the spine.
  Accepted — this is cheaper and more precise than re-specifying N type systems.
- **Open design question (handed to the spine work):** *what is the
  type-checker-agnostic contract* the spine requires from any such backend? See
  the companion analysis — the spine already only consumes `SourceAnalysis`
  (`document_symbols` + `file_occurrences` + `supertypes`), which is close to
  engine-agnostic but currently leaks `ty`/salsa lifetimes and ranges. Naming and
  hardening that contract is the real, tractable version of "make the spine type
  checker agnostic."

## 9. What we steal even though we reject the whole

1. **"Scopes as types" as a mental model** for the async type-flow refinement
   layer (CONCEPT_V2): modelling a type's members as a queryable scope is a clean
   way to think about method-level `affected` for attribute access — without
   adopting the solver.
2. **The constraint-generation / solve split** as the honest description of what
   a *type-directed* resolver must do — useful when judging whether any future
   non-`ty` backend can supply inference-flow edges, or only syntactic ones.

## 10. References

- P. Néron, A. Tolmach, E. Visser, G. Wachsmuth. *A Theory of Name Resolution*
  (scope graphs). ESOP 2015. <https://eelcovisser.org/publications/2015/NeronTVW15.pdf>
- H. van Antwerpen, P. Néron, A. Tolmach, E. Visser, G. Wachsmuth.
  *A Constraint Language for Static Semantic Analysis Based on Scope Graphs*
  (NaBL2). PEPM 2016.
- H. van Antwerpen, C. Bach Poulsen, A. Rouvoet, E. Visser. *Scopes as Types.*
  OOPSLA 2018. <https://dl.acm.org/doi/10.1145/3276484>
- Statix reference — <https://spoofax.dev/references/statix/>
- Stack graphs paper — <https://arxiv.org/abs/2211.01224>
- Companion docs — `ADR-001-stack-graphs-as-core.md`,
  `SOURCE_ANALYSIS_SEAM_ADAPTER.md`.
