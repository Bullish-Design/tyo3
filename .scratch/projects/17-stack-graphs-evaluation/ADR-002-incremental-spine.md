# ADR-002 — A generic incremental engine as tyo3's derivation spine

- **Status:** Analysis / recommendation. **Outcome: keep the hand-rolled
  `reverse_deps`→`affected`→derived-cache pipeline for now; adopt DBSP-style
  incremental view maintenance (IVM) as the *target formalism* only if/when the
  consolidation triggers in §10 fire.** Identity and MVCC are out of scope for
  any such engine and remain tyo3's own (ADR-001 §3, this doc §3, §8).
- **Date:** 2026-06-08
- **Context project:** `17-stack-graphs-evaluation`
- **Builds on:** ADR-001 (stack-graphs is not the core). This ADR answers the
  follow-up: *is there a generic incremental library/concept that could serve as
  a universal spine?*

> **TL;DR.** No single library is a "universal spine," because the spine fuses
> three separable concerns — **identity**, **versioned storage (MVCC)**, and
> **incremental derivation** — and generic frameworks address only the third.
> Within the third, there *is* a principled unification: today it is fragmented
> across salsa-inside-`ty`, rustworkx, and three hand-rolled mechanisms
> (`reverse_deps`, `affected_closure`, the Phase-8 content-addressed cache).
> **DBSP / differential dataflow** (push-based IVM) or **incremental Datalog**
> (Ascent/DDlog) could collapse the *edges → reverse-deps → affected →
> derived-staleness* pipeline into one mechanism with a soundness proof and
> *automatic* deletion handling — replacing your hand-rolled DRed. The cost is a
> heavyweight model and a hard MVCC boundary that no engine crosses. Recommended
> posture: **steal the vocabulary now, defer the engine.**

---

## 1. The question, sharpened

"Is there a generic incremental library that could be tyo3's universal spine?"
splits into two very different sub-questions, and conflating them is the trap:

- **(Q1)** Can one library subsume the *whole* spine — identity, revisions,
  snapshots, commit, derivation? — **Answer: no, and that's structural, not a
  gap.** (§3, §8.)
- **(Q2)** Can one *incremental-computation* engine unify the parts of the spine
  that are genuinely incremental computation — the L0 graph, the reverse-dep
  index, the `affected` closure, and derived-layer invalidation? — **Answer:
  yes in principle; this is the real design question, and the rest of the
  document is about it.**

## 2. What's actually in the codebase (grounding)

| Concern | Mechanism today | File |
|---|---|---|
| Incremental analysis (parse/type/symbols) | **salsa 0.26.1**, inherited from `ty_project` (the type checker's own engine — *not* tyo3's derived layers) | `rust/Cargo.toml:32-35` |
| Versioned content + snapshots | **`rpds`** persistent (structurally-shared) `HashTrieMap` + revision log | `rust/Cargo.toml:28` |
| L0 structure | native `CodeLayer { nodes, edges, reverse_deps }` | `rust/src/code_layer.rs` |
| Reverse-dep index | `add_edge`/`remove_edge` maintain `reverse_deps: BTreeMap<String, BTreeSet<String>>` | `code_layer.rs:173, 197-230` |
| Affected closure | `affected_closure` (BFS) and `affected_closure_with_deleted` (BFS + over-delete from `prev`) | `code_layer.rs:238-298` |
| Graph algorithms | rustworkx (Python) | `src/tyo3/graph/` |
| Derived invalidation | content-addressed `ArtifactCache` keyed `(input_hash, generator_version)`, put-once immutable | `src/tyo3/derive/cache.py` |

Three observations that drive everything below:

1. **`affected_closure_with_deleted` is hand-rolled DRed.** Deleting a node
   removes its inbound edges from `self.reverse_deps`, so its dependents are only
   visible in `prev.reverse_deps`; the code walks `prev` for deleted seeds
   (`code_layer.rs:281-289`). This is precisely the **Delete-and-Rederive**
   problem [11]: deletion in a recursive view can't be computed from the new
   state alone. You solved it correctly by hand; an IVM engine solves it *for
   free* as a consequence of working over change-streams with signed
   multiplicities.

2. **`ArtifactCache` is a constructive trace / content-addressed cache.** Keyed
   by `(input_hash, generator_version)`, immutable, put-once — exactly the
   "constructive trace" rebuilder and "early cutoff" of Build Systems à la Carte
   [9]. Unchanged content hash ⇒ key hit ⇒ no regeneration. You already
   implement the build-systems answer to derivation.

3. **`affected` is a transitive closure over a relation that changes
   incrementally.** That sentence is the definition of the canonical
   differential-dataflow / IVM example [5][6][8]. The fit is not analogical; it
   is literal.

## 3. Why no library is a *universal* spine (Q1)

The spine fuses three concerns that are independently well-studied and
*deliberately* separable:

1. **Durable identity & reconciliation** — stable keys across rename/move/edit,
   with the ambiguous-rebind / vanished / reappeared lifecycle
   (`tyo3-spine.allium`, `rust/src/identity.rs`). **No incremental-computation
   framework models this.** It is the same gap that sank stack-graphs (ADR-001).
   Identity is an *equivalence-and-provenance* problem, not a recomputation one.
2. **Versioned storage + snapshot isolation (MVCC)** — many readers pinned to a
   revision, one serialized writer, atomic publish. This is a database /
   persistent-data-structure concern (`rpds`, frozen overlays). Incremental
   engines maintain a *single evolving value*, not a set of versioned, isolated
   read surfaces.
3. **Incremental derivation & invalidation** — the only concern generic
   frameworks own.

**The load-bearing law (generalized from your own salsa constraint).**
CONCEPT_V2 §3 records that salsa "cannot be shared with readers or rolled back,"
forcing Strategy B+ deferred-publish. That is *not* a salsa quirk — it is the
inherent boundary between concern #3 and concern #2. An incremental engine
optimizes one mutating computation; it is structurally not a versioned
multi-reader store. **Therefore the MVCC layer must always *wrap* the
incremental engine, never be replaced by it** — true for salsa, DBSP,
differential dataflow, and Datalog alike. Any "universal spine" claim founders
here. (This is the single most important sentence in the document.)

## 4. The theory landscape (a taxonomy for Q2)

Incremental computation has three lineages. They are not competitors so much as
different points on a pull↔push, imperative↔declarative plane.

### 4.1 Demand-driven / pull (self-adjusting computation → Adapton → salsa)

- **Self-adjusting computation (SAC):** Acar, Blelloch, Harper, *Adaptive
  Functional Programming*, POPL 2002 [1]; Acar's thesis [2]. A dynamic
  dependence graph records reads; changing an input dirties a subgraph; a
  *change-propagation* pass recomputes only affected nodes. Jane Street's
  production **Incremental** (OCaml) library is the engineering embodiment [3].
- **Adapton:** Hammer et al., PLDI 2014 [4]. Adds *demand-driven* (lazy)
  recomputation via a demanded-computation graph with inner/outer dirtying —
  recompute only what is both dirty *and* demanded.
- **salsa** (Rust) [12][13]: the "red-green" algorithm — memoized tracked
  queries, automatic dependency capture, **early cutoff** (a query whose inputs
  changed but whose *output* is unchanged stops propagation), and **durability**
  tiers (skip dependency checks for inputs that rarely change). salsa's own
  README cites adapton, glimmer, and rustc's query system as inspirations.
  **You already depend on it** (via `ty`).

  *Shape:* pull. You ask for an output; the engine recomputes the minimal set of
  upstream queries. Maps naturally onto "regenerate this entity's embedding."

### 4.2 Change-propagation / push (IVM → differential dataflow → DBSP)

- **Incremental View Maintenance (IVM):** the database tradition. Blakeley et
  al.'s counting algorithm, SIGMOD 1986 [10]; Gupta & Mumick's survey [7];
  **DRed** (Delete-and-Rederive) for recursive/negated views, Gupta, Mumick,
  Subrahmanian, SIGMOD 1993 [11].
- **Differential dataflow:** McSherry, Murray, Isaacs, Isard, CIDR 2013 [6],
  atop **timely dataflow / Naiad**, Murray et al., SOSP 2013 [5]. Uses a
  *partially-ordered* timestamp lattice so iteration (recursion) and incremental
  input updates compose — you can incrementally maintain the output of an
  *iterative* computation (e.g. transitive closure) as inputs change. Rust
  crates `timely` and `differential-dataflow`; productized by **Materialize**.
- **DBSP:** Budiu, McSherry, Ryzhyk, Tannen, *DBSP: Automatic Incremental View
  Maintenance for Rich Query Languages*, VLDB 2023 / arXiv:2203.16684 [8]. The
  clean *theory* underneath differential dataflow (see §5). Productized by
  **Feldera**.

  *Shape:* push. Inputs arrive as a stream of changes (deltas); the circuit
  emits a stream of output changes. Maps naturally onto "an edge changed →
  here are the newly-affected ids."

### 4.3 Declarative-recursive (Datalog + IVM)

- **Datalog for program analysis:** **Doop** points-to analysis, Bravenboer &
  Smaragdakis, OOPSLA 2009 [14]; **Soufflé**, Jordan, Scholz, Subotić, CAV 2016
  + CC 2016 [15], a high-performance compiled Datalog. Evaluation via
  **semi-naive** fixpoint (the incremental-by-construction recursive engine).
- **Incremental Datalog:** **DDlog**, Ryzhyk & Budiu, Datalog 2.0 2019 [16] —
  Datalog where you feed input *changes* and get output *changes* (built on the
  DBSP/differential ideas). **Ascent**, Sahebolamri, Gilray, Micinski, CC 2022
  [17] — Datalog embedded in Rust via macros, "bring your own data structures."

  *Shape:* you *declare* the relations; `affected` becomes a two-rule program;
  the engine derives the incremental maintenance plan.

### 4.4 The unifying vocabulary (Build Systems à la Carte)

Mokhov, Mitchell, Peyton Jones, ICFP 2018 [9] factor *any* incremental rebuilder
into **Scheduler × Rebuilder**, with trace strategies — *dirty-bit*, *verifying
trace* (compare a hash of dependencies), *constructive trace* (content-addressed
store of results), plus **early cutoff** and **minimality**. This is the lens
that names what you already have: your `ArtifactCache` is a *constructive trace*;
the unchanged-`content_hash` skip is *early cutoff*; `affected` is your
*scheduler's* dirty set.

## 5. The strongest candidate in depth: DBSP / differential dataflow

DBSP is the cleanest formal account, so it's worth stating precisely [8].

**Z-sets.** A Z-set (Z-relation) over a domain is a function from records to
*integer* multiplicities — an element of the free abelian group ℤ[A]. Unlike
sets, Z-sets form a group: every value has an additive inverse, so a "deletion"
is just a record with multiplicity −1. Insertions and deletions are uniform.

**Streams.** A stream is an infinite sequence `s : ℕ → A` indexed by logical
time. Computations are operators on streams.

**Core operators.**
- *Lifting* `↑f`: apply a scalar function pointwise to each timestep.
- *Delay* `z⁻¹`: shift the stream by one step.
- *Differentiation* `D`: `(Ds)[t] = s[t] − s[t−1]` — the stream of *changes*.
- *Integration* `I`: running sum; `I` and `D` are mutual inverses
  (`D∘I = I∘D = id`).

**The incrementalization theorem.** For a query `Q` over snapshots, its
*incremental* version is

> `Q^Δ = D ∘ Q ∘ I`

i.e. integrate the input deltas into the current state, run `Q`, differentiate
to emit only the output deltas. The pivotal result is **compositionality** — the
"chain rule": `(Q₁ ∘ Q₂)^Δ = Q₁^Δ ∘ Q₂^Δ`. So you incrementalize a *complex*
query by incrementalizing each primitive operator and composing. Linear
operators (select, project, union) are their own incremental form; bilinear ones
(join) obey a product rule; `distinct` and recursion get specific, proven
incremental forms.

**Recursion = your `affected`.** Recursive queries (transitive closure,
reachability) are expressed with nested time domains and a fixed-point operator;
the *incremental* version of the fixpoint is exactly **semi-naive evaluation**,
and it correctly handles deletion *without a separate DRed pass* — because
deletions are just negative multiplicities flowing through the same circuit.
**This is the punchline for tyo3:** `reverse_deps` is an edge relation;
`affected = closure(changed ∪ deleted)` is its incremental transitive closure;
DBSP maintains it under insert *and* delete with one proven mechanism, retiring
the hand-rolled `affected_closure_with_deleted` over-delete logic.

**Cost / limits.** The model is heavyweight: logical timestamps, arrangements
(indexed, shared state), and an operational mental model far from a `BTreeMap`
BFS. Memory: maintaining incremental state (arrangements) costs RAM proportional
to the relations kept indexed. It buys the most when the closure is large and
churn is small — true at scale, marginal for a few-hundred-node module graph.

## 6. The most *ergonomic* candidate: incremental Datalog (Ascent)

If the goal is elegance of the *graph + affected* layer specifically, Datalog is
unbeatable on lines-of-code. The entire reverse-dep/affected logic is:

```prolog
% edges come from the producer: dep(Source, Target) for References/Imports/Inherits/Overrides
affected(X) :- changed(X).
affected(X) :- affected(T), dep(X, T).   % X depends on an affected T  ⇒ X is affected
```

Semi-naive evaluation makes this incremental by construction; DDlog [16] makes
input-delta → output-delta incremental across commits; Ascent [17] compiles it
to Rust in-process (no external toolchain, "bring your own data structures" so it
can index your existing types). **Trade:** you'd still need DRed-style deletion
handling unless you go full DDlog/DBSP-backed; Ascent's incrementality across
*separate* runs is weaker than DBSP's. And you take on a (mature, but real)
embedded-Datalog dependency and a second evaluation model beside salsa.

## 7. The candidate you already have: salsa

salsa [12][13] is demand-driven/pull, single-version, single-writer. Its
red-green algorithm already gives you early cutoff and durability. It *could*
own derived layers (an embedding is a tracked query over an entity's
content-hash; red/green ≈ your fresh/stale lifecycle). But:

- It is **pull**, so "what is affected?" is answered lazily by *asking* for each
  downstream output — the opposite of your synchronous, eager `affected` emitted
  at commit (CONCEPT_V2 §4.1). Forcing eager push out of salsa means enumerating
  and demanding every dependent, which is what `reverse_deps` already does more
  directly.
- It hits the **MVCC wall** (§3): can't share the db with readers, can't roll
  back. You already pay for this with B+ deferred-publish. Centering more of the
  spine on salsa deepens that coupling rather than resolving it.

Net: salsa is the right tool for *type analysis* (where `ty` already uses it)
and a *plausible* tool for derived-layer memoization, but the wrong shape for the
eager `affected` push, and a non-starter as the MVCC/snapshot owner.

## 8. The decision matrix

| tyo3 mechanism | Subsumable by an incremental engine? | Best fit | Verdict |
|---|---|---|---|
| Identity / reconciliation | **No** — not a recomputation problem | — | Stays tyo3's own |
| Revisions / snapshots / B+ commit | **No** — MVCC, not incrementality | `rpds` + log | Stays tyo3's own |
| Parse / type / symbols | Already incremental | salsa (via `ty`) | Keep |
| L0 edges → `reverse_deps` | Yes | DBSP / Datalog | Candidate consolidation |
| `affected` closure (+ deletion) | **Yes, with the biggest win** (retires hand-rolled DRed) | DBSP / differential | Candidate consolidation |
| Derived invalidation (`ArtifactCache`) | Already a constructive-trace cache [9] | salsa or keep | Keep; it's already correct |
| Graph algorithms (cycles, centrality) | Partially (reachability yes; others no) | rustworkx | Keep |

The consolidation opportunity is the **two middle rows**: collapse *edges →
reverse_deps → affected (incl. deletion)* into one IVM circuit. Everything else
either can't be subsumed (identity, MVCC) or is already in its right form
(content-addressed derivation, type analysis).

## 9. So would it actually be *better*? Honest trade analysis

**For (adopt DBSP-style IVM for the edges→affected pipeline):**
- One mechanism with a **soundness proof** replaces three hand-rolled pieces
  (`add_edge`/`remove_edge` bookkeeping, `affected_closure`,
  `affected_closure_with_deleted`).
- **Deletion handled for free** — the single subtlest part of the current code
  (over-delete from `prev`) disappears; signed multiplicities subsume DRed.
- Scales sub-linearly in churn: large blast radius, tiny edit ⇒ minimal work.
- A uniform substrate you could *also* express type-flow refinement edges in
  (the V2 async precision layer) as additional rules.

**Against:**
- The current code is **small, correct, fast, and proven** against the parity
  oracle. `affected_closure` is ~20 lines of BFS over a `BTreeMap`. Replacing
  proven simplicity with a heavyweight engine violates "elegance" unless the
  engine *removes* more complexity than it *adds*.
- At module-graph scale (hundreds–thousands of nodes, small per-commit churn),
  BFS is already near-optimal; DBSP's arrangement overhead may *lose* on both
  latency and memory.
- A second evaluation model (DBSP/Datalog) beside salsa is **conceptual surface
  area**, not less of it — cuts against a single-mental-model codebase.
- It changes **nothing** about the two concerns that are actually hard (identity,
  MVCC). The "universal spine" dream is not advanced one inch by this.
- DBSP/differential is a substantial dependency with its own failure modes
  (timestamp frontier management, memory growth in arrangements).

**Synthesis.** The IVM framing is *theoretically* the right description of your
affected pipeline, and if that pipeline ever becomes a correctness or
performance pain point — especially the deletion logic, or scaling to
repo-wide/multi-language graphs — DBSP-style IVM is the principled destination
and you should reach for it deliberately. Today, the hand-rolled BFS + DRed is
the *more* elegant choice because it is minimal and matches the problem size.
**Elegance here means "smallest mechanism that's provably right at this scale,"
and that is currently the code you have.**

## 10. Recommendation & triggers

1. **Keep** the hand-rolled `reverse_deps` → `affected` → `ArtifactCache`
   pipeline. It is already the build-systems-à-la-carte answer (constructive
   trace + early cutoff) plus a hand-rolled DRed that is correct.
2. **Adopt the vocabulary now** (cheap, high-value): annotate the code with the
   names — `affected_closure_with_deleted` *is* DRed [11]; `ArtifactCache` *is* a
   constructive trace [9]; `affected` *is* an incrementally-maintained transitive
   closure [6][8]. This makes the design legible and the upgrade path obvious.
3. **Defer the engine.** Re-open this ADR and reach for **DBSP / differential
   dataflow** (the `differential-dataflow` Rust crate) if **any** trigger fires:
   - the deletion/over-fire logic in `affected_closure_with_deleted` grows a
     third special case, or shows a correctness bug;
   - graphs grow past ~10⁴–10⁵ nodes (repo-wide or multi-language per ADR-001 §7)
     and BFS-per-commit shows up in profiles;
   - the V2 async type-flow refinement layer wants to be expressed as
     incrementally-maintained *rules* rather than imperative passes — at which
     point Datalog/DBSP pays for itself by unifying both edge sources.
   - you want a *single* incremental substrate for code-graph **and** derived
     staleness with one consistency proof.
4. **Never** look for an engine to own identity or MVCC. Those stay tyo3's, by
   the §3 law.

## 11. References

1. U. A. Acar, G. E. Blelloch, R. Harper. *Adaptive Functional Programming.*
   POPL 2002. <https://www.cs.cmu.edu/~rwh/papers/afp/popl02.pdf>
2. U. A. Acar. *Self-Adjusting Computation.* PhD thesis, CMU, 2005.
   <https://www.umut-acar.org/publications/acar-thesis.pdf>
3. Jane Street. *Incremental* (OCaml library) + Y. Minsky, "Seven
   Implementations of Incremental." <https://github.com/janestreet/incremental>
   · <https://blog.janestreet.com/introducing-incremental/>
4. M. A. Hammer, K. Y. Phang, M. Hicks, J. S. Foster. *Adapton: Composable,
   Demand-Driven Incremental Computation.* PLDI 2014.
   <https://www.cs.umd.edu/~hammer/adapton/adapton-pldi2014.pdf>
5. D. Murray, F. McSherry, R. Isaacs, M. Isard, P. Barham, M. Abadi. *Naiad: A
   Timely Dataflow System.* SOSP 2013.
   <https://dl.acm.org/doi/10.1145/2517349.2522738>
6. F. McSherry, D. Murray, R. Isaacs, M. Isard. *Differential Dataflow.* CIDR
   2013. <https://www.cidrdb.org/cidr2013/Papers/CIDR13_Paper111.pdf> · Rust:
   <https://github.com/TimelyDataflow/differential-dataflow>
7. A. Gupta, I. S. Mumick. *Maintenance of Materialized Views: Problems,
   Techniques, and Applications.* IEEE Data Eng. Bulletin, 1995.
   <http://sites.computer.org/debull/95JUN-CD.pdf>
8. M. Budiu, F. McSherry, L. Ryzhyk, V. Tannen. *DBSP: Automatic Incremental
   View Maintenance for Rich Query Languages.* VLDB 2023 / arXiv:2203.16684.
   <https://arxiv.org/abs/2203.16684> · Feldera: <https://www.feldera.com/>
9. A. Mokhov, N. Mitchell, S. Peyton Jones. *Build Systems à la Carte.* ICFP
   2018. <https://www.microsoft.com/en-us/research/publication/build-systems-la-carte/>
10. J. A. Blakeley, P.-Å. Larson, F. W. Tompa. *Efficiently Updating Materialized
    Views.* SIGMOD 1986. <https://dl.acm.org/doi/10.1145/16894.16861>
11. A. Gupta, I. S. Mumick, V. S. Subrahmanian. *Maintaining Views
    Incrementally* (the DRed algorithm). SIGMOD 1993.
    <https://dl.acm.org/doi/10.1145/170035.170066>
12. *Salsa* — generic on-demand incremental computation framework (Rust).
    <https://github.com/salsa-rs/salsa> · book:
    <https://salsa-rs.github.io/salsa/> · red-green algorithm:
    <https://salsa-rs.github.io/salsa/reference/algorithm.html>
13. rust-analyzer. *Durable Incrementality* (durability tiers in salsa).
    <https://rust-analyzer.github.io/blog/2023/07/24/durable-incrementality.html>
14. M. Bravenboer, Y. Smaragdakis. *Strictly Declarative Specification of
    Sophisticated Points-to Analyses* (Doop). OOPSLA 2009.
    <https://yanniss.github.io/doop-oopsla09.pdf>
15. H. Jordan, B. Scholz, P. Subotić. *Soufflé: On Synthesis of Program
    Analyzers.* CAV 2016; B. Scholz et al., *On Fast Large-Scale Program Analysis
    in Datalog*, CC 2016. <https://souffle-lang.github.io/>
16. L. Ryzhyk, M. Budiu. *Differential Datalog (DDlog).* Datalog 2.0, 2019.
    <https://github.com/vmware-archive/differential-datalog>
17. A. Sahebolamri, T. Gilray, K. Micinski. *Seamless Deductive Inference via
    Macros* / Ascent: Logic Programming in Rust. CC 2022.
    <https://github.com/s-arash/ascent> · <https://s-arash.github.io/ascent/>
18. P. Néron, A. Tolmach, E. Visser, G. Wachsmuth. *A Theory of Name Resolution*
    (scope graphs — the lineage behind stack-graphs; see ADR-001). ESOP 2015.
    <https://eelcovisser.org/publications/2015/NeronTVW15.pdf>

### tyo3 internal references
- `rust/src/code_layer.rs` — `reverse_deps`, `affected_closure`,
  `affected_closure_with_deleted` (hand-rolled DRed), `produce_code_delta_scoped`.
- `src/tyo3/derive/cache.py` — `ArtifactCache` (constructive-trace cache).
- `rust/Cargo.toml:28,32-35` — `rpds`, `salsa`.
- `.scratch/projects/15-implementation-plan/REFINED_IMPLEMENTATION_CONCEPT_V2.md`
  §3 (the salsa MVCC constraint), §4 (eager affected).
- `.scratch/projects/17-stack-graphs-evaluation/ADR-001-stack-graphs-as-core.md`.
