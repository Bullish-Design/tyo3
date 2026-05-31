# TyO3 Backend Wiring — Progress

## Status: Planning Complete

### Completed
- [x] Rust backend implementation guide created
- [x] Architecture mapped from Allium specs to Rust/PyO3
- [x] Phase plan with success criteria defined
- [x] devenv.nix configuration specified (enable Rust, maturin, env vars)
- [x] Cargo.toml with git dependencies for ty/Ruff crates
- [x] DTO layer design (all 7 model families)
- [x] Coordinate conversion strategy (1-based ↔ TextSize)
- [x] File resolution strategy (path → File handle)
- [x] PyO3 class design for TyProject
- [x] Python wrapper layer (RustProject)
- [x] Testing strategy with fixture projects
- [x] Build/packaging plan (maturin, CI, caching)
- [x] Known API quirks documented for ty 0.0.40

### Next (Phase 0 — Rust Feasibility Spike)
1. Edit `devenv.nix` to enable Rust + maturin + env vars
2. Generate `devenv shell`
3. Create `rust/Cargo.toml` with pinned ty/Ruff deps
4. Create `rust/src/lib.rs`, `dto/`, `project.rs`, `coordinates.rs`, `files.rs`
5. `cargo build` (first compile: 20–60 min)
6. Create fixture project
7. `python -c "from tyo3 import TyProject; print(TyProject.open('/tmp/fixture').document_symbols('main.py'))"`
