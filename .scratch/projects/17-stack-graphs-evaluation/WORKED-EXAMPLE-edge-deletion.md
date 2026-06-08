# Worked example — edge/node deletion: hand-rolled DRed vs. DBSP

> Companion to `PRIMER-datalog-differential-dbsp.md` and `ADR-002`. One concrete
> scenario, traced two ways: through tyo3's `affected_closure_with_deleted`
> (`rust/src/code_layer.rs`) and through the equivalent DBSP / differential
> circuit. The point is to make the **one subtle line** in the current code —
> the `prev` lookup for deleted seeds — visible as exactly the classic
> **Delete-and-Rederive (DRed)** problem, and to show what an IVM engine does
> instead.

---

## 1. The scenario

A dependency edge `X → T` means **"X depends on T"** (X references / imports /
inherits / overrides T). tyo3 stores the *reverse* index:

```
reverse_deps[T] = { X : edge X → T exists }     // "who depends on T"
```

`affected(seed)` = the seed plus everyone who transitively depends on it (the
set that must recompute). Take five code entities:

```
  D ──▶ C ──▶ B ──▶ A          edges (X depends on T):  D→C, E→C, C→B, B→A
  E ──▶ C
```

### Prior revision (`prev`)

```
prev.reverse_deps:
  A : { B }
  B : { C }
  C : { D, E }
```

Sanity check: `affected({A})` = {A, B, C, D, E} — change A and everything
upstream of it recomputes. Good.

### The commit: **delete entity C**

C is removed from the source. Every edge touching C disappears: `D→C`, `E→C`,
`C→B` are gone. Only `B→A` survives.

```
self.reverse_deps  (new revision):
  A : { B }
  B : { }          ← C was removed as a source
  (C : gone)       ← node deleted; nobody is recorded as depending on C anymore
```

Commit inputs: `seeds = changed ∪ deleted = { C }`, `deleted = { C }`.

**The correct answer:** D and E *depended on* C, so deleting C affects them.
`affected = { C, D, E }`.

---

## 2. Path A — tyo3's hand-rolled closure

The naive closure walks `self.reverse_deps` only:

```
queue=[C], visited={}
  pop C → visited={C};  self.reverse_deps[C] = ∅  → enqueue nothing
done →  affected = { C }          ❌ MISSES D and E
```

The miss is structural: C's node is gone from `self`, so the edges that recorded
"D and E depend on C" are gone too. **The dependents of a deleted node survive
only in the prior layer.** That is precisely the line in
`affected_closure_with_deleted` (`code_layer.rs:281-289`):

```rust
// A deleted id's dependents are only in the prior layer.
if deleted.contains(&id) {
    if let Some(sources) = prev.reverse_deps.get(&id) {   // ← consult prev
        for src in sources { /* enqueue */ }
    }
}
```

Re-run with the `deleted` branch:

```
queue=[C], visited={}
  pop C → visited={C}
        self.reverse_deps[C] = ∅
        C ∈ deleted → prev.reverse_deps[C] = { D, E }  → enqueue D, E
  pop D → visited={C,D};  self.reverse_deps[D] = ∅;  D ∉ deleted
  pop E → visited={C,D,E}; self.reverse_deps[E] = ∅;  E ∉ deleted
done →  affected = { C, D, E }    ✅ correct
```

**What made it correct:** an explicit, hand-written rule that says "for a deleted
seed, read the *old* state." This is **DRed by hand** — the over-delete/rederive
insight (Gupta–Mumick–Subrahmanian, SIGMOD 1993): deletion in a recursive view
cannot be computed from the new state alone; you must consult the pre-deletion
graph. The code is correct and ~6 lines, but the correctness rests on the author
*knowing* this subtlety and special-casing it. Add negation, or layered
derivations, and the special cases multiply.

---

## 3. Path B — the DBSP / differential circuit

Model the same thing relationally. Two input Z-sets and one recursive view:

```prolog
% inputs (EDB)
dep(X, T)        % the dependency edges
changed(X)       % this commit's directly changed/deleted seeds

% standing incremental view (IDB) — transitive reachability over dep
reaches(X, T) :- dep(X, T).
reaches(X, T) :- dep(X, M), reaches(M, T).

% the per-commit answer
affected(X) :- changed(X).
affected(X) :- changed(T), reaches(X, T).
```

### State as a Z-set (each fact carries a `+1` multiplicity)

Before the commit, the maintained `reaches` view holds (multiplicities all `+1`):

```
reaches:  D→C, D→B, D→A,  E→C, E→B, E→A,  C→B, C→A,  B→A
```

### The commit arrives as **signed diffs**, not a new snapshot

Deleting C is expressed as retractions (`−1`) plus the seed:

```
Δdep:      −(D→C),  −(E→C),  −(C→B)
Δchanged:  +C
```

### What the circuit does (conceptually)

1. **Integration `I`** reconstructs the *current* `dep` relation by accumulating
   all diffs seen so far. Crucially, at the instant the deletion is processed,
   the integrated state **still contains** `D→C` and `E→C` — they are only now
   being cancelled. So the engine has not "forgotten" who depended on C.

2. **The recursive `reaches` view is maintained incrementally.** The `−1` diffs
   flow through the bilinear `join` (product rule:
   `Δ(dep ⋈ reaches) = Δdep ⋈ reaches + dep ⋈ Δreaches + Δdep ⋈ Δreaches`) and
   the `distinct`. Every `reaches` fact whose *only* support routed through C is
   retracted with a `−1`:

   ```
   Δreaches:  −(D→C), −(D→B), −(D→A),  −(E→C), −(E→B), −(E→A),  −(C→B), −(C→A)
   ```

   `B→A` survives (its support never used C). No prior-layer lookup was written
   by hand: the retraction is a mechanical consequence of pushing negatives
   through the same operators that handle insertion. **This is DRed for free.**

3. **`affected` for the commit** is read off the (now-updated) view joined with
   the seed `changed = {C}`: who *reached* C? The retraction stream itself names
   them — `D` and `E` (the sources of the `−(·→C)` diffs) — giving

   ```
   affected = { C, D, E }    ✅ same answer
   ```

### The crucial contrast

The hand-rolled path needs an explicit branch — *"if this seed was deleted, go
read `prev`."* The DBSP path needs **no such branch**: integration keeps the old
edges available during the transition, and signed diffs make a deletion just an
insertion with the sign flipped. The "consult the old state" step is *absorbed
into the algebra* (`I` reconstructs it; `D` emits the delta), with a soundness
proof that it is correct for arbitrary recursive — and even non-monotone —
queries.

---

## 4. Side by side

| Aspect | Hand-rolled `affected_closure_with_deleted` | DBSP / differential circuit |
|---|---|---|
| How deletion is expressed | a `deleted` set + a special branch | a `−1` diff, identical machinery to insertion |
| Where "old dependents" come from | explicit `prev.reverse_deps` lookup | integration `I` keeps them live during the delta |
| Correctness argument | author knows the DRed subtlety; reviewed by hand | theorem: `Q^Δ = D∘Q∘I`, sound for recursion + negation |
| Code size (this case) | ~6 extra lines, ~20 total | a 4-rule program + an engine |
| Cost at small scale | O(blast radius) BFS over a `BTreeMap`; tiny | arrangement/index overhead can exceed the BFS |
| Cost at large scale / high churn | re-walks per commit; fine until graphs are huge | work ∝ change size; wins as graph ≫ change |
| New requirement: negation/aggregation | new hand-written special cases (fragile) | already handled by signed Z-sets |
| Second derived layer over `affected` | another bespoke maintenance pass | another rule; composes by the chain rule |
| Identity / MVCC | unaffected (tyo3 owns these regardless) | unaffected (engine cannot own these — ADR-002 §3) |

---

## 5. The lesson (ties back to ADR-002)

Both produce `{ C, D, E }`. The difference is *where the deletion subtlety
lives*:

- **Hand-rolled:** in a correct-but-load-bearing special case that a future
  editor must not break, and that grows new cases as the derivation graph grows.
- **DBSP/DD:** dissolved into the algebra, with a proof, at the price of a
  heavyweight engine and indexed state.

At tyo3's present scale (module graphs of hundreds–thousands of nodes, small
per-commit churn) the hand-rolled BFS is the **more** elegant choice — it is the
smallest mechanism that is provably right *here*, and the DBSP arrangement
overhead can lose on both latency and memory. The calculus flips when any
ADR-002 §10 trigger fires:

- the deletion logic accretes a *third* special case (or shows a bug);
- graphs reach ~10⁴–10⁵ nodes (repo-wide / multi-language, ADR-001 §7);
- a second incrementally-derived layer wants to compose over `affected`;
- the V2 async type-flow refinement edges want to be *rules*, not passes.

At that point the right move is the `differential-dataflow` Rust crate (DD as
engine) or a DBSP-backed Datalog (DDlog / Ascent), maintaining
`dep → reaches → affected` as one circuit — and deleting this hand-rolled DRed
branch entirely.

---

## References

See `PRIMER-datalog-differential-dbsp.md` §References. Most directly:

- DRed: Gupta, Mumick, Subrahmanian, *Maintaining Views Incrementally*, SIGMOD
  1993. <https://dl.acm.org/doi/10.1145/170035.170066>
- DBSP: Budiu, McSherry, Ryzhyk, Tannen, VLDB 2023 / arXiv:2203.16684.
  <https://arxiv.org/abs/2203.16684>
- Differential dataflow: McSherry, Murray, Isaacs, Isard, CIDR 2013.
  <https://github.com/TimelyDataflow/differential-dataflow>
- tyo3 code: `rust/src/code_layer.rs` —
  `affected_closure_with_deleted` (lines ~263-298).
