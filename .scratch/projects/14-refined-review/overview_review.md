Deep Review: TyO3 (post-Gate-8) — Code, Intern Review, and the Gate 3N Question

1. Bottom line up front

Three documents are on the table and they do not all agree:

- The intern's review (14-refined-review/) says the spine has contract mismatches and recommends, as its preferred end
state, "move canonical commit delta and code-layer state into Rust, make Python CodeGraph a projection."
- The Gate 3N architectural analysis says "Yes, implement Gate 3N" — which is exactly that same move.
- The Gate 3N recovery analysis says "do nothing, the current code already meets the correctness property via 
revision-gating."

After verifying against the actual source, my conclusion is:

▎ The intern's review and the Gate 3N architectural analysis are correct and are describing the same destination from two 
▎ directions. The Gate 3N recovery analysis is wrong on the merits — the correctness property is not already met. You 
▎ should refactor toward the Gate 3N architecture, but framed not as an isolated "performance gate" but as the unifying fix
▎ for the spine-level defects the intern found.

Given your stated goal (cleanest/most elegant, effort no object), the answer is unambiguous. I'll justify each part.

2. What I verified in the actual code

Every one of these I confirmed by reading the source, not by trusting the review:

┌────────────────────────┬───────────────┬─────────────────────────────────────────────────────────────────────────────┐
│        Finding         │    Status     │                                  Evidence                                   │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ Snapshots read live    │ ✅ Confirmed  │ build_frozen → pre_populate_generation (project.rs:1122) walks the live     │
│ disk                   │ — most        │ OsSystem (:1073) and interns whatever disk says now for any file missing    │
│                        │ serious       │ from the generation (:1102-1108).                                           │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ Write transaction      │ ✅ Confirmed  │ session.py:951-952 etc. — every write calls _apply_graph_delta +            │
│ split Rust↔Python      │               │ _publish_delta after the native Mutex released.                             │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ Read accessor commits  │ ✅ Confirmed  │ session.graph → CodeGraph.build → _prime_identity_registry → sync_all()     │
│ a revision             │               │ (graph.py:34-41, :184). A property read advances head.                      │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│                        │               │ dto/sync.rs:1-19 documents created/changed/deleted as paths, moved as       │
│ Delta is path-shaped,  │ ✅ Confirmed  │ qualified-paths, needs_review/orphaned as DurableIds — all flattened to     │
│ not id-level           │               │ untyped Vec<String>. Spec §4 requires created/changed/deleted to be         │
│                        │               │ DurableIds.                                                                 │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ Derived invalidation   │               │ session.py:758 builds dirty from result.created|changed (paths); dag.py:139 │
│ is silently dead       │ ✅ Confirmed  │  iterates them as durable_id; resolve_input raises → swallowed at :146 →    │
│                        │               │ no-op. Plus :165 leaks an unclosed snapshot.                                │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ discard() never        │ ✅ Confirmed  │ session.py:1013 calls _apply_graph_delta but omits _publish_delta —         │
│ publishes to bus       │               │ diverged because every write method copy-pastes the sequence.               │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ Convenience views over │ ✅ Confirmed  │ session.code/layer/entity (:1308-1330) close the snapshot then return the   │
│  closed snapshots      │               │ lazy view.                                                                  │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ Hash is                │ ✅ Confirmed  │ hash.rs:113 normalizes line-by-line. collapse_whitespace runs inside string │
│ text-heuristic, not    │ + extra bug   │  literals, so "a  b"→"a b" (a meaning change) does not change the hash —    │
│ AST                    │               │ violates §7.2.3.                                                            │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ Python re-parses       │               │ _read_coordination_config (session.py:763) re-reads TOML, except Exception: │
│ config with silent     │ ✅ Confirmed  │  pass at :788.                                                              │
│ fallback               │               │                                                                             │
├────────────────────────┼───────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ No Python write lock   │               │ No Lock/RLock guards the write path. Yet bus.py:78-96 documents a           │
│ at all (my finding)    │ ✅ Confirmed  │ write-lock guarantee and warns at runtime when revisions arrive out of      │
│                        │               │ order — a guarantee the architecture cannot keep.                           │
└────────────────────────┴───────────────┴─────────────────────────────────────────────────────────────────────────────┘

The intern's review is accurate, well-evidenced, and high quality. I found essentially no false positives.

3. Why the "do nothing" recovery doc is wrong

The recovery analysis claims the current Python path "achieves [§3.3.1] through the revision-gated apply pattern." Three
independent reasons that's false, all verified:

1. pre_populate_generation breaks the §1 content gate with zero concurrency. A time-travel snapshot at R reads files from
today's disk for anything not already interned. Two snapshots at the same R can disagree. This is the foundational gate
everything else rests on, and it's broken single-threaded.
2. The split transaction breaks §3.3.3 with zero concurrency. Between commit_head returning and Python finishing
_apply_graph_delta, a reader observes content@R with graph@R−1.
3. Under real concurrency it corrupts, and the code knows it. With no Python write lock, two writer threads can publish bus
deltas out of revision order (bus's own defensive log fires) and apply graph deltas out of order — the revision gate then
"recovers" by rebuilding through current head, silently dropping the intermediate revision's graph state. The
architectural-analysis doc admits this ("revision 3's graph state is silently dropped"). That is data loss, not
correctness.

So "revision-gating already satisfies the invariants" is not true. It papers over ordering while losing state and doing
nothing for content authority, snapshot consistency, or bus ordering.

4. The core architectural diagnosis

Strip away the individual findings and there is one root cause:

▎ Ownership of "what revision R contains" is split across the Rust Mutex and unsynchronized Python. The Rust side owns
▎ content + identity; Python owns the code graph, derived invalidation, and the bus — all outside the lock, with no lock of
▎ their own.

Every P0/P1 is a symptom:
- pre_populate exists because generations aren't authoritative, so the snapshot builder backfills from disk.
- The path-shaped delta exists because the id-level code layer lives in Python, so Rust can only report the paths it
touched.
- _prime_identity_registry exists because the graph builder needs identity that should have been reconciled at commit.
- The derived/bus bugs exist because post-commit hooks are hand-rolled per write method outside any transaction.

This is precisely what Gate 3N targets, and precisely the intern's "Recommended Architecture Decision."

5. Recommendation: adopt the Gate 3N direction — as the spine refactor, not a side gate

Target end state (the most elegant version):

▎ One revision = one native transaction, under one lock, over complete content, producing one id-level delta that already
▎ includes the code-layer structural changes. Python holds only read-only projections of published revisions plus
▎ integrations (generators, stores, bus queues, pydantic models).

Concretely this means:
1. Rust owns committed content fully. Ingest disk at open/sync/watch/commit time; delete pre_populate_generation; the
frozen overlay stays strict (no disk fallback). (Fixes P0.1; restores the §1 gate.)
2. Rust owns the canonical code layer (the CodeLayer + reverse_deps from code_layer.rs), updated inside the commit lock —
this is Gate 3N proper. (Fixes P0.2/§3.3.1/§3.3.3.)
3. Rust emits one CommitDelta that is id-level (created_ids/changed_ids/deleted_ids/moved[{id,old,new}]/affected_ids +
touched_files as metadata + rescan) and carries the structural CodeDelta. (Fixes P0.4/P0.5; revives derived invalidation 
P1.3.)
4. Python CodeGraph becomes a pure applier/projection of CodeDelta — zero FFI during apply, no read-surface build, no
_prime_identity_registry. (Fixes P0.3; deletes ~1,200 lines.)
5. One post-commit path. A single _after_commit(delta) consumed by every writer, so discard cannot diverge. (Fixes P1.1.)
6. Then the cleanups: AST-canonical hashing (P1.5), transactional sidecar rollback (P0.6), eager/owned snapshot lifetimes
for convenience APIs (P1.4), single config source (P1.7), typed errors instead of swallowing (P1.6), zero warnings.

On the gate3n-reference files specifically: do not import them — both gate3n docs agree, and I concur.
code_layer.rs/code_delta.rs never compiled and target a removed Entity shape. Use the guide (its 8-step plan +
parity-oracle pattern is sound), rewrite against current types. The code_delta.rs wire contract (§4 above) is a good design
template; re-author it with the id-level field names from the intern's CommitDelta so the two converge into one DTO rather
than two competing ones.

6. Where I'd go further than either document

- Treat it as one refactor, sequenced as the intern's guide lays out (content gate → id-delta → read purity → transaction 
boundary → ...). Gate 3N done alone would fix the lock boundary but leave pre_populate (P0.1) and the path-delta (P0.4) —
and the recovery doc's framing of Gate 3N as an optional "performance gate" is what let it be skipped last time. Frame it
as the spine, not an optimization.
- The bus's own defensive comment is a design smell worth acting on: code that documents a lock it doesn't hold and logs
when its invariant breaks is telling you the invariant belongs upstream. Once publish happens inside the native commit's
tail, the comment becomes true and the defensive check becomes a real assertion.
- The repetitive write methods are a structural hazard, not just verbosity. Eight near-identical try/except/apply/publish
blocks already produced the discard bug. Collapsing them behind one native commit(mutation) + one _after_commit removes a
whole bug class.

7. Net verdict

- Intern review: accurate, thorough, and its "Recommended Architecture Decision" is right. Adopt it; the refactoring
guide's phase ordering is sound.
- Gate 3N: yes — but as the centerpiece of the spine refactor, merged with the content-gate and id-delta fixes, not as a
standalone gate. The architectural-analysis doc is correct; the recovery-analysis doc's "do nothing" is wrong.
- Cleanest end state: Rust owns committed truth (content + identity + code layer) and emits one id-level delta per
revision; Python is a faithful read-only projection plus integrations. That single principle dissolves nearly every finding
at once.

If you'd like, I can turn this into a concrete, sequenced implementation plan (a corrected merge of the intern's phases +
the Gate 3N steps into one branch plan), or start on Phase 1 (making committed generations complete and killing
pre_populate_generation). Want me to proceed?