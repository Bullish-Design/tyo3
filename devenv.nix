{ pkgs, lib, config, inputs, ... }:

{
  env.GREET = "devenv";

  packages = [ 
    pkgs.git 
    pkgs.uv
    pkgs.maturin         # Build Python extensions with PyO3
  ];

  # ── Languages (Rust + Python) ──────────────────────────────
  # NOTE: languages.rust.enable = true provisions rustup, cargo, rustc automatically.
  # Do NOT add pkgs.rustup to packages — it would conflict with the devenv Rust module.
  languages = {
    rust.enable = true;
    python = {
      enable = true;
      version = "3.13";
      venv.enable = true;
      uv.enable = true;
    };
  };

  # ── Environment variables for Cargo ─────────────────────────
  env.CARGO_NET_GIT_FETCH_WITH_CLI = "true";  # Use system git for crate fetching
  env.RUST_BACKTRACE = "1";                    # Debug Rust panics

  # ── Build scripts ────────────────────────────────────────────

  scripts.hello.exec = ''
    echo "tyo3 dev shell — $GREET"
  '';

  scripts.build.exec = ''
    echo "═══ Building Rust extension (debug) ═══"
    cd "$DEVENV_ROOT/rust"
    cargo build 2>&1
    cp target/debug/lib_native_impl.so "$DEVENV_ROOT/src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so"
    echo "═══ Build complete — .so copied to src/tyo3/ ═══"
  '';

  scripts.build-release.exec = ''
    echo "═══ Building Rust extension (release) ═══"
    cd "$DEVENV_ROOT/rust"
    cargo build --release 2>&1
    cp target/release/lib_native_impl.so "$DEVENV_ROOT/src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so"
    echo "═══ Release build complete — .so copied to src/tyo3/ ═══"
  '';

  scripts.build-wheel.exec = ''
    echo "═══ Building maturin wheel ═══"
    mkdir -p "$DEVENV_ROOT/dist"
    cd "$DEVENV_ROOT"
    maturin build --release --out dist/ 2>&1
    echo "═══ Wheel built — see dist/ ═══"
    ls -lh dist/tyo3-*.whl
  '';

  # ── Test scripts ─────────────────────────────────────────────

  scripts.test.exec = ''
    echo "═══ Running all tests ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest src/tyo3/tests/ -v --tb=short --cov=tyo3 --cov-report=term-missing 2>&1
  '';

  scripts.test-quick.exec = ''
    echo "═══ Running quick tests (no native extension needed) ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest src/tyo3/tests/ -q \
      --ignore=src/tyo3/tests/test_rust_integration.py \
      --ignore=src/tyo3/tests/test_rust_snapshots.py \
      --ignore=src/tyo3/tests/test_coordinate_conversion.py \
      --ignore=src/tyo3/tests/test_property_based.py \
      --ignore=src/tyo3/tests/test_rust_performance.py 2>&1
  '';

  scripts.test-rust.exec = ''
    echo "═══ Running Rust backend tests ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest src/tyo3/tests/test_rust_integration.py src/tyo3/tests/test_rust_snapshots.py src/tyo3/tests/test_coordinate_conversion.py -v --tb=short 2>&1
  '';

  scripts.test-property.exec = ''
    echo "═══ Running property-based tests (Hypothesis) ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest src/tyo3/tests/test_property_based.py -v --tb=short 2>&1
  '';

  scripts.test-perf.exec = ''
    echo "═══ Running performance benchmarks ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest src/tyo3/tests/test_rust_performance.py -v -s --tb=short 2>&1
  '';

  scripts.test-coverage.exec = ''
    echo "═══ Running all tests with coverage report ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest src/tyo3/tests/ --cov=tyo3 --cov-report=term-missing --cov-report=html 2>&1
    echo "═══ HTML coverage report: $DEVENV_ROOT/htmlcov/index.html ═══"
  '';

  scripts.test-ci.exec = ''
    echo "═══ Running CI-style test suite ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest src/tyo3/tests/ -x -q --tb=short --cov=tyo3 --cov-report=term-missing 2>&1
  '';

  # ── Utility scripts ──────────────────────────────────────────

  scripts.clean.exec = ''
    echo "═══ Cleaning build artifacts ═══"
    cd "$DEVENV_ROOT/rust"
    cargo clean 2>&1
    rm -f "$DEVENV_ROOT/src/tyo3/_native_impl"*.so
    rm -rf "$DEVENV_ROOT/dist"
    rm -rf "$DEVENV_ROOT/htmlcov"
    echo "═══ Clean complete ═══"
  '';

  scripts.check-so.exec = ''
    echo "═══ Checking native extension ═══"
    LS="$(ls -lh "$DEVENV_ROOT/src/tyo3/_native_impl"*.so 2>/dev/null)"
    if [ -n "$LS" ]; then
      echo "$LS"
      PYTHONPATH="$DEVENV_ROOT/src" python -c "
from tyo3.rust_project import RustProject
rp = RustProject('$DEVENV_ROOT/fixtures/simple_package')
files = rp.files()
symbols = rp.document_symbols('main.py')
rp.close()
print(f'✅ Extension works — {len(files)} file(s), {len(symbols)} symbol(s)')
"
    else
      echo "❌ No native extension found — run: devenv shell -- build"
    fi
  '';

  scripts.status.exec = ''
    echo "═══ TyO3 dev environment status ═══"
    echo ""
    echo "  Rust:"
    echo "    rustc: $(rustc --version 2>/dev/null || echo 'not found')"
    echo "    cargo: $(cargo --version 2>/dev/null || echo 'not found')"
    echo ""
    echo "  Python:"
    echo "    python: $(python --version 2>/dev/null || echo 'not found')"
    echo "    uv:     $(uv --version 2>/dev/null || echo 'not found')"
    echo ""
    echo "  Build:"
    echo "    maturin: $(maturin --version 2>/dev/null || echo 'not found')"
    LS="$(ls -lh "$DEVENV_ROOT/src/tyo3/_native_impl"*.so 2>/dev/null | head -1)"
    if [ -n "$LS" ]; then
      echo "    native:  $LS"
    else
      echo "    native:  not built (run: build)"
    fi
    WHL="$(ls "$DEVENV_ROOT/dist/tyo3-*.whl" 2>/dev/null | head -1)"
    if [ -n "$WHL" ]; then
      echo "    wheel:   $(basename $WHL)"
    fi
    echo ""
    echo "  Available commands:"
    echo "    build              — build debug + copy .so"
    echo "    build-release      — build release + copy .so"
    echo "    build-wheel        — build maturin wheel"
    echo "    test               — full test suite"
    echo "    test-quick         — unit tests only (no native needed)"
    echo "    test-rust          — Rust backend integration tests"
    echo "    test-property      — Hypothesis property-based tests"
    echo "    test-perf          — performance benchmarks"
    echo "    test-ci            — CI-style (fail-fast) test run"
    echo "    check-so           — verify native extension works"
    echo "    clean              — remove all build artifacts"
  '';

  # ── Shell entry / test ───────────────────────────────────

  enterShell = ''
    hello
    status
  '';

  enterTest = ''
    echo "Running CI-style tests..."
    cd "$DEVENV_ROOT/rust"
    cargo build 2>&1
    cp target/debug/lib_native_impl.so "$DEVENV_ROOT/src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest src/tyo3/tests/ -x -q --tb=short 2>&1
  '';
}
