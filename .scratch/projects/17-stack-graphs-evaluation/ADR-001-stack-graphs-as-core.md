# ADR-001 — Stack graphs as the tyo3 core building block

- **Status:** Rejected (as *core*); accepted as a candidate *implementation of one delegated contract*, gated on a scope condition (§7).
- **Date:** 2026-06-08
- **Context project:** `17-stack-graphs-evaluation`
- **Deciders:** architecture
- **Supersedes / relates to:** the `SourceAnalysis` delegation boundary in `tyo3-spine.allium`; the V2 `affected`-precision model in `15-implementation-plan/REFINED_IMPLEMENTATION_CONCEPT_V2.md` (§0 finding 2).

> One-line verdict: **stack-graphs solves name binding; tyo3's core is identity +
> revisions + cross-layer consistency.** Name resolution is a *consumer* of the
> substrate, not its foundation. Stack-graphs can implement part of one delegated
> contract inside L0 — it cannot be the spine, and adopting it would *regress*
> the one axis (`affected` precision) we actually care about.

---

## 1. The question

Would [stack-graphs](https://github.com/github/stack-graphs) be a better core
building block for tyo3 than the current design (native L0 `CodeLayer` fed by a
`ty`-based `SourceAnalysis` engine, maintaining a reverse-dependency index that
emits a transitive `affected` set)? Refactoring cost and fork-maintenance are
**explicitly out of scope** for this decision — we evaluate on architectural
elegance and fit alone.

References studied:
- Blog: <https://github.blog/open-source/introducing-stack-graphs/>
- Paper: <https://arxiv.org/abs/2211.01224>
- Repo: <https://github.com/github/stack-graphs> (archived read-only, Sept 2025)

## 2. What stack-graphs actually is

A descendant of Eelco Visser's **scope graphs** (TU Delft). Its entire universe
is one question: *which definition(s) does this reference resolve to?*

- **One graph.** Definition nodes (pop a symbol), reference nodes (push a
  symbol), scope nodes, push/pop-symbol and push/pop-scoped-symbol nodes, a
  shared **root**, and a **jump-to-scope** node.
- **Two stacks during search.** A **symbol stack** (the dotted name being
  resolved, `a.b.c`) and a **scope stack** (type-directed / `import` lookups and
  jumps). A reference→definition path is valid iff both stacks balance;
  **edge precedence** models shadowing.
- **Partial paths.** Each file is analysed in *complete isolation* into
  precomputed path fragments. At query time those fragments are **stitched**
  across files through the shared root. This is the whole incrementality story:
  edit a file → recompute only its partial paths → never touch dependents or
  dependencies; all cross-file work is deferred to query time.
- **Construction.** Declarative, via tree-sitter + the graph-construction
  language (TSG). No build system, no compiler, **no type checker. Purely
  syntactic.**
- **Scope.** "Good-enough" navigation (go-to-def / find-refs) across *every*
  language at GitHub scale with zero per-repo config. Explicitly **does not** do
  type inference, dataflow, or value tracking. Allowed to return multiple
  candidate definitions; errs toward over-listing.

## 3. What tyo3 actually is

From the specs, the spine is three invariants stack-graphs has no concept of:

1. **Durable identity** — a minted `DurableId` stable across renames, moves, and
   content edits, with a reconciliation lifecycle (ambiguous re-bind, vanished,
   reappeared). `tyo3-spine.allium`.
2. **Revisioned snapshot isolation** — frozen, revision-pinned read surfaces;
   the B+ deferred-publish atomic commit. `tyo3-spine` + `tyo3-coordination`.
3. **Cross-layer consistency** — one revision, every layer (code L0, derived
   embeddings/docs, authored records) agreeing, anchored by id and `ContentHash`.
   `tyo3-layers.allium`.

The code graph is *one layer* (L0). Its primary job in the architecture is to
feed the **reverse-dep index** (`CodeLayer.reverse_deps`,
`affected_closure_with_deleted`) that produces the transitive,
container-granular `affected` set so derived layers know what to recompute.
**Name resolution is a consumer of the substrate, not its foundation.**

## 4. The comparison on the dimensions that matter

| Dimension | tyo3 needs | stack-graphs provides |
|---|---|---|
| **Durable cross-revision identity** | The keystone — embeddings/docs/authored records survive edits | **Nothing.** Node handles are intra-file and recomputed every edit. No stable anchor for "this function." |
| **Atomic revisioned commit / MVCC snapshots** | Spine invariant | **Nothing.** A partial-path DB keyed by file; no revisions, no snapshot isolation. |
| **Derived + authored layers** | Reason the project exists | **Nothing.** Out of universe. |
| **Structural (named) edges for reverse-dep / affected** | Yes — `References`/`Imports`/`Inherits`/`Overrides` | **Yes** — a resolved binding *is* a structural edge. Genuine overlap (see §6). |
| **Inference-flow edges** (`w = make_widget(); w.draw()`) | The documented miss class (CONCEPT_V2 §0.2) | **Same hole, made worse.** Purely syntactic — no inference at all. A real type checker can in principle do better here; stack-graphs cannot, ever. |
| **Incremental model** | Synchronous `affected` *at commit* | **Conflicts.** Its elegance is *laziness* — defer cross-file stitching to query time. tyo3 needs the reverse-dep closure *eagerly*, in-commit. |
| **Language-agnostic, no build** | Python-centric; *wants* `ty`'s type info | Its headline strength — irrelevant-to-counterproductive here. |

## 5. The decisive arguments

1. **It contributes zero to the actual core (rows 1–3).** Identity, revisions,
   and the derived/authored layers are the spine and the reason tyo3 exists.
   Stack-graphs has no vocabulary for any of them. It cannot be "the core"; the
   most it can be is an implementation detail of L0's name-binding step.

2. **It regresses the one axis we care about (row 5).** V2 already discovered
   that our engine doesn't expose *inference-flow* edges, so method-level
   `affected` has holes we cover at container granularity. Stack-graphs has the
   identical limitation **by design**, and is strictly weaker: it has no type
   inference to recover from, whereas the `ty`-based engine at least *could*.
   The project name — **Ty**O3 — is the tell: we invested in a real type checker
   on purpose. Adopting a syntactic-only resolver trades *down* on precision to
   buy language-agnosticism we don't currently need.

3. **Its incremental philosophy fights ours (row 6).** Stack-graphs is elegant
   precisely because it is *lazy* — nothing cross-file happens until a query
   stitches partial paths. tyo3's whole commit contract is *eager*: emit a
   transitive `affected` set synchronously so the bus and derived layers can
   react. Bolting eager reverse-deps onto a lazy formalism discards the part
   that makes stack-graphs clean.

## 6. The genuine overlap (and why it's not enough)

A resolved name-binding *is* a structural edge, so stack-graphs and tyo3 agree
on exactly one thing: turning a reference occurrence into a
`source_id → target_id` fact. That maps onto the `compute_file_occurrences →
NameOccurrenceDto → References/Imports edge` step in `produce_code_delta_scoped`.
This is real, and it is the *only* seam where stack-graphs could plug in (see
the companion doc `SOURCE_ANALYSIS_SEAM_ADAPTER.md`). It replaces ~15% of one
delegated contract, supplies the *same* nominal edges we already get, and brings
*none* of the type-directed precision. Net architectural gain at the seam:
roughly zero; net precision change: negative.

## 7. The one condition that would flip this decision

The entire verdict rests on tyo3's goal being a **Python-centric, type-aware,
identity-anchored knowledge substrate** (which every spec and the `ty`
dependency assert). Stack-graphs becomes genuinely central in exactly one
alternate world:

> **If the real destination is massively-multi-language, navigation-first,
> precision-second code intelligence at GitHub scale** — many languages, zero
> per-repo config, "good-enough" go-to-def over compiler-grade correctness —
> then stack-graphs' formalism is the most elegant known core, and this ADR
> should be revisited.

Nothing studied suggests that is the destination. If it ever becomes one,
re-open this ADR before Phase planning.

## 8. Decision

- **Reject** stack-graphs as the tyo3 *core building block*.
- **Keep** the current spine (identity, revisions, snapshots, B+ commit) and the
  native L0 `CodeLayer` + reverse-dep index.
- **Permit** stack-graphs (or the TSG construction language) as a *future,
  optional implementation of the name-binding portion of `SourceAnalysis`* —
  only if/when the multi-language condition in §7 holds. Documented at the seam
  in `SOURCE_ANALYSIS_SEAM_ADAPTER.md`.

## 9. Consequences

- **Positive:** the spine's identity/revision invariants stay the conceptual
  centre; no precision regression; no impedance mismatch between lazy stitching
  and eager `affected`.
- **Negative / accepted:** tyo3 remains tied to a per-language analysis engine
  (`ty` for Python). Multi-language expansion will require either new engines or
  re-opening §7. We accept this — language reach is not a current goal.
- **Ideas adopted regardless** (see §10).

## 10. What we steal even though we reject the whole

1. **The partial-path / file-isolation formalism** as a *mental model* for
   incremental structural analysis. We've independently arrived at a cousin
   (`produce_code_delta_scoped`: re-derive only the dirty scope ∪ one-hop
   importers, maintain edges incrementally). The instructive contrast — and the
   right choice for us — is **eager reverse-deps vs. lazy stitch-at-query**.
   *Action:* add a paragraph to CONCEPT_V2 recording *why* eager wins for tyo3
   (synchronous `affected` is a hard requirement; laziness would fight it).
2. **The TSG declarative graph-construction language** — the cleanest known way
   to get language-agnostic structural edges without writing N extractors.
   *Action:* note it as the reference design if/when §7's condition is ever met.

## 11. References

- Stack graphs blog — <https://github.blog/open-source/introducing-stack-graphs/>
- Stack graphs paper — <https://arxiv.org/abs/2211.01224>
- Scope graphs (lineage) — Visser et al., TU Delft
- tyo3 specs — `.scratch/specs/tyo3-{spine,layers,coordination,sidecar}.allium`
- V2 concept — `.scratch/projects/15-implementation-plan/REFINED_IMPLEMENTATION_CONCEPT_V2.md`
- Companion — `SOURCE_ANALYSIS_SEAM_ADAPTER.md`
