# Changelog

All notable changes to TyO3 will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — Unreleased

### Added
- `TyO3Session` — single public API class for cursor-style code intelligence (check, check_file, document_symbols, workspace_symbols, goto_definition, goto_declaration, goto_type_definition, find_references, semantic_tokens, file_occurrences, type_hierarchy, hover).
- `CodeGraph` — RustworkX-backed semantic code graph with symbol nodes, reference edges, dependency analysis, import cycle detection, coupling metrics, and export (JSON, DOT).
- Rust native extension (`tyo3._native_impl`) built with PyO3 0.28 on ty/Ruff v0.0.40.
- Pydantic models for symbols, diagnostics, navigation results, and graph payloads.
- GIL released during heavy ty/Salsa analysis via `py.allow_threads`.
- Comprehensive test suite (integration, snapshot, property-based, performance, concurrency).
- CI pipeline with Ruff lint, Rust clippy + rustfmt, and full test gates.

[0.1.0]: https://github.com/BullishDesign/tyo3/releases/tag/v0.1.0
