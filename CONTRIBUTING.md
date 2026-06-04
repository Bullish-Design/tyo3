# Contributing to TyO3

## Development environment

TyO3 uses [devenv](https://devenv.sh) to provision a reproducible development
shell with the exact Rust toolchain, Python 3.13, and all build dependencies.

```bash
# Enter the development shell
devenv shell

# Build the Rust native extension (debug)
devenv shell -- build

# Run the full test suite
devenv shell -- tests

# Run Rust-specific tests
devenv shell -- test-rust

# Run property-based tests
devenv shell -- test-property

# Run performance benchmarks
devenv shell -- test-perf

# Lint and format
devenv shell -- ruff check src
devenv shell -- ruff format src

# Clean build artifacts
devenv shell -- clean
```

## Pre-commit gates

Before opening a PR, run these gates and ensure all pass:

```bash
devenv shell -- ruff check src
devenv shell -- ruff format --check src
devenv shell -- cargo fmt --manifest-path rust/Cargo.toml --check
devenv shell -- cargo clippy --manifest-path rust/Cargo.toml -- -D warnings
devenv shell -- build
devenv shell -- tests
devenv shell -- test-rust
devenv shell -- test-property
```

## Bumping the ty/Ruff dependency

TyO3 pins ty/Ruff to a specific git revision in `rust/Cargo.toml`. This is
**intentional** — ty is a fast-moving compiler and API surfaces change between
revisions. Bumping is a deliberate, tested operation:

1. Identify the target ty release and its pinned Ruff submodule revision
   (e.g., ty v0.0.40 → ruff rev `3cb09eba`).
2. Update **all eight** `ty_*` / `ruff_*` crate revisions in `rust/Cargo.toml`
   to the same commit.
3. Rebuild with `devenv shell -- build` and fix any compilation errors.
4. Run the full test suite: `devenv shell -- tests && devenv shell -- test-rust && devenv shell -- test-property`.
5. If any snapshot tests fail, review the changes and update snapshots as needed.
6. Commit the bump with a message like:
   `build: bump ty/Ruff deps to v0.0.XX (ruff rev XXXXXXX)`

## Architecture notes

- The Rust extension (`rust/src/`) wraps ty/Ruff's semantic engine behind a
  PyO3 bridge. DTO conversion lives in `rust/src/convert/`; Python ↔ Rust
  marshalling uses `pythonize`.
- `TyO3Session` (in `src/tyo3/session.py`) is the **only** public API class.
- The `CodeGraph` (in `src/tyo3/graph/`) builds on the session cursor APIs to
  provide whole-project queries.
- Zero `unsafe` in the PyO3 layer. All native methods take `py: Python<'_>`.
- The GIL is released during heavy ty/Salsa work via `py.allow_threads`.

## Project structure

```
src/tyo3/           — Python package
  session.py        — TyO3Session (public API)
  exceptions.py     — Public exception hierarchy
  models/           — Pydantic models
  graph/            — CodeGraph (RustworkX semantic graph)
  tests/            — Test suite
rust/               — Rust native extension
  src/
    lib.rs          — PyO3 module registration
    project.rs      — PyTyProject (the pyclass)
    convert/        — DTO converters (Rust → Pythonize dicts)
    dto/            — Internal DTO structs
    coordinates.rs  — LineIndex / offset conversion
fixtures/           — Test fixture projects
```

## License

MIT — see [LICENSE](LICENSE).
