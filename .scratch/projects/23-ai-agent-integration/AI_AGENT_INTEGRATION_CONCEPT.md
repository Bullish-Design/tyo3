# AI / LLM-Agent Integration — Concept & Brainstorm

> **Status:** concept / brainstorm. No commitments. The goal is to map the design
> space for putting LLMs and coding agents *on top of* TyO3's spine, and to pick a
> small number of high-leverage experiments.
>
> **Thesis in one line:** TyO3 is not "another index" — it is a *reactive,
> identity-stable substrate* (durable ids + attachable layers + a delta bus +
> MVCC time-travel + traced read-sets + a typed daemon API). That is an unusually
> good fit for the four things LLM agents are *bad* at on raw code: stable
> handles, durable memory, incremental awareness, and trustworthy caching.

---

## 0. Why TyO3 is a good agent substrate (the primitives, and what each unlocks)

| TyO3 primitive | What it is | What it unlocks for an agent |
|---|---|---|
| **Durable identity** | a `DurableId` that survives edits, reformats, and atomic moves | A *stable handle* an agent can reason about and annotate across a whole session/history. No more "line 42 of file X" that rots on the next edit. |
| **Authored layers** | free-form / schema'd values keyed by durable id, with `history` + `review_on_change` | **Persistent agent memory glued to code identity.** Intent, risk notes, TODOs, "I already reviewed this," coverage gaps — and they go `needs_review` automatically when the entity changes. |
| **Derived layers** | content-addressed artifacts produced by a generator (`python`/`command`/`http`) or a `Producer` object | **LLM-derived intelligence as a first-class, cached, honest-stale citizen.** Summaries, docstrings, embeddings, complexity/security notes. |
| **AB2 traced read-sets** | a derived value is keyed by *exactly what the producer read* | **Correct-by-construction caching + self-heal.** The summary of `checkout` recomputes when (and only when) something it actually depends on changes. The minimal-correct context for a prompt is *computable*. |
| **AB3 async serve (`serving="stale"`)** | slow producers run off the actor; serve last-good now, publish `DerivedFresh` when ready | **Slow LLM/HTTP calls never block.** The agent UI stays responsive; the value pops in. |
| **Delta bus** (delta / refinement / derived channels) | push notifications with the *id-level affected closure* of each commit | **Reactive, incremental agents.** "On every change, here is the exact, minimal set of entities to re-analyze" — no whole-repo re-scan. |
| **Affected closure** | transitive blast radius of an edit, narrowed by precision=method | **Targeted work + review scoping.** Re-summarize / re-check / re-test only what an edit can affect. |
| **MVCC snapshots / time-travel** | pin a revision; diff two revisions at the *entity* level | **Reproducible prompts** (build context at a pinned rev) and **diff-aware review** ("what changed *semantically* since I last looked"). |
| **The daemon JSON-RPC API** | `entity_at`, `references`, `hover`, `rename`, `derived`, `author`, `diff`, `decorate`, `layer_ids`, `type_hierarchy`, … over a unix socket | **A ready-made agent tool surface.** This is ~80% of an MCP server already. |

The recurring theme: **agents are stochastic and forgetful; TyO3 is deterministic
and has memory.** Pair them so the LLM does the fuzzy reasoning and TyO3 owns the
stable state, the caching, and the "what changed."

---

## 1. The flagship: TyO3 → MCP server ("give your agent an identity-stable codebase")

**What:** wrap the daemon's verbs as a [Model Context Protocol](https://modelcontextprotocol.io)
server so *any* LLM agent (Claude, etc.) can navigate and annotate the codebase
through durable identity instead of grepping text.

**Why it's the flagship:** the daemon already speaks structured JSON-RPC over a
socket and already has the right verbs. An MCP bridge is mostly a protocol
adapter + good tool descriptions. It turns every capability below into something
a hosted agent can use *today*, with no model training.

**Tool shapes (sketch):**
- `find_entity(path, line, col) -> {durable_id, qualified_name, kind, …}` — the stable handle.
- `entity_card(durable_id) -> {identity, authored, derived, affected}` — everything attached.
- `references(durable_id)`, `callers(durable_id)`, `type_hierarchy(durable_id)` — structured navigation.
- `read_note(layer, durable_id)` / `write_note(layer, durable_id, value)` — **durable agent memory.**
- `derive(layer, durable_id)` — pull an LLM-derived summary/embedding (cached).
- `diff(from_rev, to_rev)` — entity-level semantic diff.
- `affected_of(durable_id)` / subscribe to the bus — "what does changing this touch?"

**The wow:** an agent can write `intent`/`risk`/`reviewed_at` notes keyed by
durable id, and *they survive the next refactor*. The agent's memory of the
codebase is no longer a flat text scratchpad that rots — it's glued to identity
and goes `needs_review` when the code moves under it.

---

## 2. LLM-derived layers (the closest to "already built")

The derived-layer contract is `generate(inputs: list[GenInput]) -> list[bytes]`,
batched, content-addressed, with `serving`/`key_locality`/`generator_version`. An
LLM call is just a slow generator. Examples:

- **`summary` (NL):** "what does this function do" — one sentence per entity.
- **`docstring`:** a proposed docstring (authored-on-accept; see §6).
- **`embedding`:** vector per entity → semantic search via `snap.nearest(q, k)`
  (a vector store layer already exists in the config vocabulary).
- **`risk` / `complexity` / `security`:** a classifier pass; surfaces as a
  diagnostic-severity badge (ties into the Native-LSP work — see the sibling
  prototype).
- **`test_ideas`:** suggested test cases for the entity.
- **`explain_change`:** keyed on a *diff*, not an entity — "what did this commit do."

**Why TyO3 makes these *good* and not just possible:**
- **Content-addressed cache = don't pay twice.** Unchanged code ⇒ no LLM call.
  This is the single most important cost lever. `generator_version` busts the
  cache when you change the prompt.
- **AB2 traced keys = self-heal.** A `summary` that read a callee's signature
  recomputes when that signature changes — automatically.
- **AB3 async serve = non-blocking.** `serving="stale"` is *the* mode for LLM
  layers: serve the old summary instantly, refresh in the background, pop in.
- **Honest staleness vocabulary** (`fresh|stale|computing|failed|absent`) is
  exactly the confidence signal an agent (or a human) needs.

**Generator transport options that already exist:** `type="python"` (in-process),
`type="command"` (subprocess, e.g. a CLI that calls an API), `type="http"`
(endpoint). So an LLM layer can be wired with **zero engine changes** — just a
config block + a small generator. The AB1 registration API also allows a
programmatic `Producer` for the traced-read-set path.

---

## 3. Reactive / incremental agents (subscribe to the bus)

An agent subscribes to the delta bus (the daemon already pushes `delta` /
`refinement` / `derived`). On each commit it receives the **id-level affected
closure**. Instead of re-running over the whole repo, it runs only on what
changed:

- **Incremental reviewer:** on commit, for each `affected_id`, re-run a "does this
  still look right?" pass; write findings as `review` notes; flip stale ones.
- **Incremental doc-keeper:** re-summarize only affected entities.
- **Incremental test-gap finder:** when an entity changes and its `coverage` note
  says "untested," nudge.
- **Guardrail agent:** subscribe to a specific `intent`/`invariant` layer; when an
  edit touches an entity carrying "load-bearing: do not inline," raise a warning
  *at edit time* (the bus delivers the affected set synchronously enough).

This is the differentiator vs. "run a linter agent on the whole repo": **work is
proportional to the change, not the repo size**, and it's *targeted* by real
dependency edges (AB2/method-precision), not heuristics.

---

## 4. Context assembly / RAG over the spine

LLM answer quality is dominated by context quality. TyO3 can assemble a
**structured, minimal, reproducible** context pack:

- **Identity-anchored:** "the entity at the cursor + its callees' signatures + its
  authored notes + its derived summary + its affected closure." Deduped by
  durable id, not by text chunk.
- **Minimal-correct via traced read-sets (AB2):** the set of things a producer
  *actually read* is a principled answer to "what context does this entity need?"
- **Token-budgeted:** walk the graph outward from the anchor, include
  signatures/summaries (cheap) before full bodies (expensive), stop at budget.
- **Reproducible:** build the pack against a **pinned MVCC revision** so a prompt
  is replayable and a cached answer is valid for exactly that revision.

Compare to naive embedding-RAG: this is *graph-aware* and *identity-stable*, so it
doesn't retrieve three stale copies of a moved function or miss a caller.

---

## 5. Time-travel / diff-aware agents

`snap.diff(old, new)` returns entity-level `{added, removed, changed, moved}`.
Agents can:
- **Review only the semantic delta:** "what changed since rev N" in terms of
  *entities*, not text hunks — fewer tokens, no formatting noise.
- **Track an entity through history** by durable id: "show me how `checkout`
  evolved," even across moves/renames.
- **Regression-aware annotations:** an authored `last_reviewed_rev` lets an agent
  ask "what's changed since I approved this?" and re-review just that.

---

## 6. Agent-authored edits with identity-safe provenance

When an agent proposes/applies an edit:
- Attach **provenance** as an authored layer: `{author: "agent-x", reason: "...",
  prompt_rev: ...}` keyed by durable id. It rides the entity through later moves.
- Use the **affected closure** as the review scope: "this agent edit can affect
  these N entities — here's the blast radius to check."
- Run a **verification layer** (type-check / tests) over the affected closure and
  feed failures back → tight **generate → verify → iterate** loop, all incremental.

This is the "agent that edits your code but you can *trust the review surface*"
story — the spine gives provenance + blast radius for free.

---

## 7. Semantic search & structure mining (embedding layer)

With an `embedding` derived layer + the vector store + `nearest()`:
- **Semantic code search:** "find functions like this one."
- **Duplicate / near-duplicate detection** → refactor candidates.
- **Clustering** for "these 8 functions are the auth subsystem" → auto-grouping
  for docs/onboarding.
- **Drift detection:** an entity whose embedding moved a lot between revisions =
  "this changed meaning, not just text."

---

## 8. Architecture notes / how to actually wire it

- **Where the LLM call lives:** a derived-layer generator (`python`/`command`/
  `http`) or an AB1-registered `Producer`. Keep it **`serving="stale"`** (AB3) so
  it's off the hot path.
- **Cost control is a caching problem, and caching is solved:** content-addressed
  keys mean an unchanged entity never re-calls the model. Batch via the
  `generate(list)->list` contract. Set `generator_version` = a hash of the prompt
  template so prompt edits invalidate cleanly.
- **Determinism:** LLM output isn't deterministic, but the *artifact* is stored
  and keyed by input hash — so a given (code, prompt_version) maps to one cached
  answer. Re-generation only on real input change. This makes LLM intelligence
  behave like a pure function of (content, prompt).
- **Privacy / locality:** the `http`/`command` transports let you point at a local
  model (Ollama/vLLM) vs a hosted API per layer. Sensitive layers stay local.
- **Latency budget:** async serve + the `computing` UI cue (see the Native-LSP /
  spinner work) keeps the experience honest under multi-second calls.
- **The MCP bridge** is a separate process that holds a daemon client and exposes
  tools — it does not need engine changes.

---

## 9. Risks & open questions

- **Cost blow-ups** if cache keys are too coarse (e.g. a `semantic`/transitive key
  that thrashes on every commit). Prefer `local`/traced keys for LLM layers; only
  go semantic when the value genuinely depends on the closure.
- **Prompt/version drift:** without `generator_version` discipline, stale answers
  served as `fresh`. Make prompt-template hashing a convention.
- **Trust & hallucination:** LLM-derived layers must render with honest status and
  never masquerade as ground truth (the existing `fresh|stale|failed` UI already
  helps; agent-authored notes should carry provenance, §6).
- **MCP tool ergonomics:** durable ids are opaque strings; tool descriptions must
  teach the agent the `find_entity → durable_id → everything else` flow, or it'll
  fall back to grep.
- **Bus backpressure under an agent firehose:** a chatty subscriber must use the
  bounded/coalescing queue (already the bus contract) and not block writers.

---

## 10. Ranked experiments (cheap → deep)

1. **`summary` LLM layer via `type="http"` pointed at a local model**, `serving="stale"`.
   Zero engine changes; proves the cache + async + pop-in end-to-end with a *real* model.
   *(We already demo a fake `blurb` slow layer — swap the sleep for an API call.)*
2. **MCP bridge (read-only first):** `find_entity`, `entity_card`, `references`,
   `derive`, `diff`. Lets a hosted agent navigate by identity. Highest leverage.
3. **Durable agent-memory notes:** `intent`/`risk` authored layers written by the
   agent via MCP; show `needs_review` lifecycle. The "memory that survives refactor" wow.
4. **Reactive incremental reviewer:** subscribe to the bus, re-review the affected
   closure on each commit, write notes. Proves "work ∝ change."
5. **Context-pack endpoint:** a daemon verb that returns a token-budgeted,
   identity-anchored, pinned-revision context pack for a durable id.
6. **Embedding layer + semantic search** (`nearest`), then dup-detection.
7. **Generate → verify loop:** agent edit + provenance note + verification layer
   over the affected closure.

**Recommended first two:** (1) a real LLM `summary` layer to validate the
production path with a live model, and (2) the **read-only MCP bridge** — together
they turn the existing spine into "an identity-stable codebase your agent can read
and remember," which is the whole pitch.

---

## Appendix: mapping daemon verbs → agent capabilities

| Daemon verb (exists today) | Agent capability |
|---|---|
| `entity_at` | resolve cursor/locus → durable id |
| `decorate` | enumerate a file's entities + their layer state |
| `references`, `document_highlights` | find callers / usages |
| `type_hierarchy` | supertypes/subtypes |
| `hover` | type/signature/doc for a locus |
| `derived` | pull an LLM-derived artifact (cached, honest-stale) |
| `author` / `authored` | write / read durable agent memory |
| `layers` / `layer_ids` | discover what intelligence is attached |
| `diff` | entity-level semantic diff across revisions |
| `rename` / `can_rename` | compute identity-aware edits |
| bus `subscribe` (per-layer, AB7) | react to exactly the changes you care about |
