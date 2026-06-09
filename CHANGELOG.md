# Changelog

All notable changes to TyO3 will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-06-08

The **spine refactor**: TyO3 becomes an incremental, transactional semantic
engine. Rust owns committed truth; Python is a pure read/projection surface.

### Added
- **Editable, transactional sessions** — `session.edit` / `edit_many` overlay
  edits in memory (no disk writes); each write is one atomic native commit that
  fully rolls back on failure (no torn publish).
- **MVCC snapshots** — `session.snapshot()` pins an immutable, revision-stamped,
  thread-shareable view; snapshots are built from a frozen database (no live-disk
  reads) and support time-travel via `snapshot(at=rev)`.
- **Durable identity** — stable `DurableId`s that survive cosmetic edits and
  atomic moves; AST-canonical content hashing so a hash changes only on a
  meaningful edit (the container-subsumes-members invariant is tested).
- **Id-level commit delta** — every write returns a `CommitDelta` with
  `created_ids` / `changed_ids` / `deleted_ids` / `moved` and the transitive,
  container-granular `affected_ids` closure, computed natively at the source.
- **Derived layers** — content-hash-keyed derived artifacts with `local` vs
  `semantic` key locality (a local layer does not recompute on a dependency-only
  change; a semantic layer does), pluggable stores, and self-healing caches.
- **Authored layers** — durable, identity-keyed authored records with history and
  needs-review / orphaned lifecycle, captured per snapshot.
- **Delta bus** — subscribe to ordered, id-level change notifications by interest;
  an optional async `precision = method` refinement channel narrows the
  container-granular affected set to method precision and degrades gracefully.
- Single validated config source (native), surfaced to Python via `TyConfig`.

### Changed
- Reads are pure projections — no read accessor advances head; `session.graph`
  and snapshot graphs are projections of a native code delta, not read-surface
  walks.
- `project.rs`, `session.py`, and `graph/graph.py` split into focused
  packages/modules (`project/`, `session/`, `graph/projection.py` + mixins).

### Internal
- Zero-warning gate: `cargo clippy --all-targets -- -D warnings`, `ruff check`,
  and `ruff format --check` all clean; an end-to-end acceptance suite proves the
  full lifecycle.

## [0.1.0] — Unreleased

### Added
- `TyO3Session` — single public API class for cursor-style code intelligence (check, check_file, document_symbols, workspace_symbols, goto_definition, goto_declaration, goto_type_definition, find_references, semantic_tokens, file_occurrences, type_hierarchy, hover).
- `CodeGraph` — RustworkX-backed semantic code graph with symbol nodes, reference edges, dependency analysis, import cycle detection, coupling metrics, and export (JSON, DOT).
- Rust native extension (`tyo3._native_impl`) built with PyO3 0.28 on ty/Ruff v0.0.40.
- Pydantic models for symbols, diagnostics, navigation results, and graph payloads.
- GIL released during heavy ty/Salsa analysis via `py.allow_threads`.
- Comprehensive test suite (integration, snapshot, property-based, performance, concurrency).
- CI pipeline with Ruff lint, Rust clippy + rustfmt, and full test gates.

[0.2.0]: https://github.com/BullishDesign/tyo3/releases/tag/v0.2.0
[0.1.0]: https://github.com/BullishDesign/tyo3/releases/tag/v0.1.0
