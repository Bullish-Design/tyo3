# Primer — Datalog, Differential Dataflow, and DBSP

> A self-contained conceptual overview of the three incremental-computation
> technologies evaluated in `ADR-002-incremental-spine.md`. Companion worked
> example: `WORKED-EXAMPLE-edge-deletion.md`.
>
> The throughline: **don't recompute from the state; compute from the changes.**
> These three sit at different layers — **Datalog is a *language* (what to
> compute), semi-naive evaluation is *how to compute it bottom-up*, and
> differential dataflow / DBSP are *how to maintain the answer incrementally as
> inputs change*.** We thread one example — transitive closure / reachability —
> through all three, because it is both the canonical case and exactly tyo3's
> `affected` set.

---

## 1. Datalog — the declarative language

Datalog is a query language from the intersection of Prolog and relational
databases: Prolog stripped of function symbols and ordering, which makes it
**decidable and guaranteed to terminate** [A1][A2]. You write *facts* and
*rules*; the engine computes everything entailed.

**Facts** (the EDB — *extensional* database, raw inputs):

```prolog
edge(a, b).
edge(b, c).
edge(c, d).
```

**Rules** (the IDB — *intensional* database, derived):

```prolog
% reachability / transitive closure
tc(X, Y) :- edge(X, Y).            % base case
tc(X, Y) :- edge(X, Z), tc(Z, Y).  % recursive case
```

Read `:-` as "if". A rule head holds whenever its body (a conjunction) has a
satisfying assignment. The whole language is conjunction, recursion, and
projection — no loops, no mutation, no order.

**Semantics: the least fixpoint.** A program's meaning is the *smallest* set of
facts closed under the rules: apply rules until nothing new is derivable. This
is well-defined because pure Datalog is **monotone** — adding a fact can only
*add* conclusions, never retract them — so a unique least fixpoint exists
(Knaster–Tarski) [A1].

**Why it matters.** You declare *what* relationships hold; the engine works out
*how*. This is transformative for program analysis — points-to analysis, call
graphs, type inference, and name resolution are all recursive relational
queries. **Doop** (points-to) [A3] and **Soufflé** (a Datalog→C++ compiler) [A4]
are flagship industrial analyzers written as a few hundred rules.

**Two extensions that matter.**

- **Negation** breaks monotonicity (adding a fact can *remove* a conclusion).
  The standard fix is **stratified negation**: layer the program so a negated
  relation is fully computed before any rule negates it.
- **Aggregation** (`count`, `min`, `sum`) is likewise non-monotone and stratified.

**Evaluation: naive vs. semi-naive — incrementality's first appearance.**

- *Naive*: each round, re-derive **all** facts from **all** facts so far.
  Correct but wildly redundant.
- *Semi-naive*: track the **delta** — facts newly derived in the previous round
  — and only fire rules that consume at least one new fact (a rule firing on
  only-old facts already produced its conclusion). For `tc`, round *n* extends
  paths by one edge using paths found in round *n−1* [A1][A2].

Semi-naive is "incremental *within* a single fixpoint." It is the seed of
everything below.

---

## 2. Differential dataflow — incremental *and* iterative at once

Semi-naive makes the *initial* computation efficient. The harder problem is
what happens *after inputs change* — an edge added or removed. Classic
incremental view maintenance (IVM) handles non-recursive views; recursion is
nasty, because deleting one edge can invalidate an unbounded set of derived
paths (the **DRed** / Delete-and-Rederive problem) [A5][A6].

Differential dataflow (DD; McSherry, Murray, Isaacs, Isard [B1]), built on the
**timely dataflow / Naiad** engine [B2], does **incremental updates and
iteration simultaneously** — a combination almost nothing else achieves. Most
incremental systems can't express recursion; most iterative systems can't accept
incremental input.

**Data model: collections as streams of signed changes.** A collection is
represented by its *changes*, each a triple

```
(record, time, diff)
```

where `diff` is a **signed integer** multiplicity. `+1` inserts, `−1` deletes.
The collection "as of time T" is the sum of diffs with time ≤ T. Signed diffs
make deletion uniform with insertion — never a special case, just a negative.

**Key innovation: partially-ordered timestamps.** Two kinds of progress happen
at once: **outer epochs** (real-world input updates over time) and **inner
iterations** (rounds of a fixpoint). A single counter can't distinguish "new
because input changed" from "new because we iterated again," so DD timestamps
form a **lattice** — typically the product `(epoch, iteration)` ordered
component-wise. The coordinates advance independently, and the engine tracks a
moving **frontier** to know when a result is final. This lattice is Naiad's deep
contribution: it lets an incremental input change re-enter a running iterative
computation and redo only the affected slices of both dimensions [B1][B2].

**Operators:** `map`, `filter`, `join`, `reduce`/`count`, `distinct`, `concat`,
and crucially **`iterate`** (the fixpoint combinator).

**Arrangements:** indexed, incrementally-maintained, *shareable* state (like
materialized sorted indexes). Joins read arrangements; operators share them.
This is where DD spends memory — the cost of reacting cheaply to any future
change.

**Transitive closure in DD** (schematically):

```rust
edges.iterate(|reach| {
    reach.join(&edges).map(|(_z, x, y)| (x, y))  // extend paths one hop
         .concat(&edges).distinct()              // include direct edges, dedup
})
```

Add an edge → DD recomputes only paths through it. **Remove** an edge → the `−1`
diff propagates through `join` and `distinct`, retracting exactly the dependent
paths: **DRed solved structurally, no separate delete pass.** Work is
proportional to the change's blast radius, not the graph size. Productized by
**Materialize** as streaming SQL [B3].

---

## 3. DBSP — the clean theory underneath

DD works, but its lattice machinery is operationally heavy and hard to reason
about formally. **DBSP** (Budiu, McSherry, Ryzhyk, Tannen — VLDB 2023 [C1])
recovers the same power with far simpler math, and underpins **Feldera** [C2].
It makes "incrementalize anything" a *theorem*.

**Streams.** A stream is an infinite sequence indexed by time, `s : ℕ → A`. A
computation is an operator mapping streams to streams.

**Z-sets (the data model).** A Z-set over `D` is a function `D → ℤ` — each
record carries an *integer* multiplicity. Z-sets form an **abelian group**: they
add, and every Z-set has an additive inverse, so deletion is a `−1` record and
insert/delete are unified. (Ordinary sets form only a lattice — no inverses —
which is exactly why classic IVM struggles with deletion.)

**Four operators suffice:**

- **lift** `↑f` — apply `f` to each timestep independently;
- **delay** `z⁻¹` — shift the stream one step (`(z⁻¹s)[t] = s[t−1]`, `s[−1]=0`);
- **differentiation** `D` — `(Ds)[t] = s[t] − s[t−1]` (the stream of *changes*);
- **integration** `I` — the running sum; `I` and `D` are exact inverses
  (`D∘I = I∘D = id`).

`D` turns states into deltas; `I` turns deltas back into states. That inverse
pair is the whole engine.

**The incrementalization theorem.** For any snapshot query `Q`, its incremental
version — consuming input deltas, emitting output deltas — is

> **Q^Δ = D ∘ Q ∘ I**

Integrate incoming deltas to reconstruct the current input, run ordinary `Q`,
differentiate the output to emit only changes. *Any* `Q` can be incrementalized
this way [C1].

**The chain rule (why it's practical).** Incrementalization **distributes over
composition**:

> **(Q₁ ∘ Q₂)^Δ = Q₁^Δ ∘ Q₂^Δ**

So you incrementalize a complex query by incrementalizing each *primitive* and
composing. The primitives have proven incremental forms:

- **Linear** operators (select, project, union, map, filter) are *their own*
  incremental form — `(↑f)^Δ = ↑f`; changes pass straight through.
- **Bilinear** operators (join) obey a **product rule**: the change in `a ⋈ b`
  is `Δa ⋈ b + a ⋈ Δb + Δa ⋈ Δb`, using integrated ("current") values on the
  non-delta sides. Work ∝ change sizes, not input sizes.
- **`distinct`** and **recursion** have specific proven incremental forms.

**Recursion = semi-naive, for free.** Recursive queries use *nested* streams (a
stream whose timesteps are fixpoint iterations). Mechanically incrementalizing
the fixpoint with the rules above **yields exactly semi-naive evaluation** — and
because Z-sets are signed, it handles deletion with no separate DRed phase. DBSP
*derives* the algorithm Datalog people hand-wrote, as a corollary of the chain
rule [C1].

DBSP replaces DD's partial-order lattice with totally-ordered *nested* streams
and proves the same results with undergraduate algebra — which is why it is the
reference *theory* even though DD is the mature *engine*.

---

## 4. How the three fit together

```
LANGUAGE        Datalog          "what relationships hold"   tc(X,Y) :- edge(X,Z), tc(Z,Y).
                   │ compiles to
ALGORITHM       semi-naive       "least fixpoint, no redundant rederivation"
                   │ generalized by
INCREMENTAL     differential     ENGINE: signed diffs + lattice timestamps (incremental + iterative)
ENGINE/THEORY    dataflow ─ DBSP THEORY: Z-sets + Q^Δ = D∘Q∘I + chain rule  ⟹  semi-naive for free
```

Concrete systems:

- **DDlog** — Datalog compiled to differential dataflow (write rules, get
  incremental maintenance) [D1].
- **Feldera** — SQL/streaming compiled to DBSP circuits [C2].
- **Ascent** — Datalog embedded in Rust via macros, in-process, "bring your own
  data structures" [D2].
- **Materialize** — differential dataflow as a streaming SQL database [B3].

---

## 5. Why this maps onto tyo3 (see ADR-002 and the worked example)

tyo3's entire `affected` machinery is one two-rule Datalog program:

```prolog
affected(X) :- changed(X).
affected(X) :- affected(T), dep(X, T).   % X depends on an affected thing ⇒ X is affected
```

- `dep(X, T)` is `reverse_deps` (built from References/Imports/Inherits/Overrides).
- `affected` is the least fixpoint — the `affected_closure` BFS in
  `rust/src/code_layer.rs`.
- The deletion case hand-coded in `affected_closure_with_deleted` (walk `prev`
  for deleted seeds) is the **DRed** problem; in DBSP/DD it vanishes — a deleted
  edge is a `−1` diff that retracts its consequences automatically.

ADR-002's recommendation stands: at current scale the hand-rolled BFS is the
*more* elegant choice (smallest mechanism that's provably right); DBSP-style IVM
is the principled destination if the graph grows, the deletion logic accretes
special cases, or the V2 type-flow refinement layer wants to be expressed as
incrementally-maintained rules. See `WORKED-EXAMPLE-edge-deletion.md` for a
side-by-side trace.

---

## References

### Datalog / semi-naive / program analysis
- [A1] S. Abiteboul, R. Hull, V. Vianu. *Foundations of Databases* (the "Alice
  book"), ch. 12–13 (Datalog, fixpoint, semi-naive). Addison-Wesley, 1995.
  <http://webdam.inria.fr/Alice/>
- [A2] J. D. Ullman. *Principles of Database and Knowledge-Base Systems*, Vol. I
  (naive/semi-naive evaluation). Computer Science Press, 1988.
- [A3] M. Bravenboer, Y. Smaragdakis. *Strictly Declarative Specification of
  Sophisticated Points-to Analyses* (Doop). OOPSLA 2009.
  <https://yanniss.github.io/doop-oopsla09.pdf>
- [A4] H. Jordan, B. Scholz, P. Subotić. *Soufflé: On Synthesis of Program
  Analyzers.* CAV 2016. <https://souffle-lang.github.io/>

### Incremental view maintenance / DRed
- [A5] A. Gupta, I. S. Mumick. *Maintenance of Materialized Views: Problems,
  Techniques, and Applications.* IEEE Data Eng. Bulletin, 1995.
  <http://sites.computer.org/debull/95JUN-CD.pdf>
- [A6] A. Gupta, I. S. Mumick, V. S. Subrahmanian. *Maintaining Views
  Incrementally* (the DRed algorithm). SIGMOD 1993.
  <https://dl.acm.org/doi/10.1145/170035.170066>

### Differential dataflow / timely / Naiad
- [B1] F. McSherry, D. Murray, R. Isaacs, M. Isard. *Differential Dataflow.*
  CIDR 2013. <https://www.cidrdb.org/cidr2013/Papers/CIDR13_Paper111.pdf> ·
  Rust: <https://github.com/TimelyDataflow/differential-dataflow>
- [B2] D. Murray, F. McSherry, R. Isaacs, M. Isard, P. Barham, M. Abadi.
  *Naiad: A Timely Dataflow System.* SOSP 2013.
  <https://dl.acm.org/doi/10.1145/2517349.2522738>
- [B3] Materialize (differential dataflow as streaming SQL).
  <https://materialize.com/>

### DBSP
- [C1] M. Budiu, F. McSherry, L. Ryzhyk, V. Tannen. *DBSP: Automatic Incremental
  View Maintenance for Rich Query Languages.* VLDB 2023 / arXiv:2203.16684.
  <https://arxiv.org/abs/2203.16684>
- [C2] Feldera (DBSP runtime). <https://www.feldera.com/> ·
  <https://github.com/feldera/feldera>

### Incremental Datalog in practice
- [D1] L. Ryzhyk, M. Budiu. *Differential Datalog (DDlog).* Datalog 2.0, 2019.
  <https://github.com/vmware-archive/differential-datalog>
- [D2] A. Sahebolamri, T. Gilray, K. Micinski. *Ascent: Logic Programming in
  Rust.* CC 2022. <https://github.com/s-arash/ascent> ·
  <https://s-arash.github.io/ascent/>

### Cross-references
- `ADR-002-incremental-spine.md` — the decision and trade analysis.
- `WORKED-EXAMPLE-edge-deletion.md` — DRed-by-hand vs DBSP side by side.
- `rust/src/code_layer.rs` — `reverse_deps`, `affected_closure`,
  `affected_closure_with_deleted`.
