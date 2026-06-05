{ pkgs, lib, config, inputs, ... }:

let
  # Verbose, noisy output for chasing a specific failure: DEBUG cli-logging,
  # locals in tracebacks, full tracebacks, live stdout/stderr capture. This is
  # OPT-IN — pass `--detail` to any test script to enable it.
  pytestFullLogArgs = "--strict-markers -vv --verbosity=2 --tb=long --showlocals --show-capture=all --capture=tee-sys -o log_cli=true --log-cli-level=DEBUG";
  # Lean default: quiet, short tracebacks. Fast to run and read.
  pytestLeanArgs = "--strict-markers -q --tb=short";
  # Per-test default: lists every test with its PASSED/FAILED result, short
  # tracebacks. Verbose enough to see each outcome, still no debug noise.
  pytestPerTestArgs = "--strict-markers -v --tb=short";
  pytestDefaultMarkerArgs = "-m \"not benchmark\"";

  # Shared prelude for test scripts. Selects a lean default or the full output
  # flags into $PYTEST_LOG_ARGS, and strips `--detail` from "$@" so the
  # remaining args still pass through to pytest. The default flags are
  # parameterized so the top-level `tests` runner can default to per-test
  # output while the focused scripts stay quiet. Usage in a script:
  #   ${detailPrelude}            # quiet default
  #   ${detailPreludePerTest}     # per-test PASSED/FAILED default
  #   ... python -m pytest $PYTEST_LOG_ARGS <fixed args> "$@" ...
  mkDetailPrelude = defaultArgs: ''
    PYTEST_LOG_ARGS="${defaultArgs}"
    _detail_filtered=()
    for _arg in "$@"; do
      case "$_arg" in
        --detail) PYTEST_LOG_ARGS="${pytestFullLogArgs}" ;;
        *) _detail_filtered+=("$_arg") ;;
      esac
    done
    set -- "''${_detail_filtered[@]}"
  '';
  detailPrelude = mkDetailPrelude pytestLeanArgs;
  detailPreludePerTest = mkDetailPrelude pytestPerTestArgs;
in
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
    cd "$DEVENV_ROOT"
    maturin develop 2>&1
    echo "═══ Build complete ═══"
  '';

  scripts.build-release.exec = ''
    echo "═══ Building Rust extension (release) ═══"
    cd "$DEVENV_ROOT"
    maturin develop --release 2>&1
    echo "═══ Release build complete ═══"
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

  scripts.tests.exec = ''
    ${detailPreludePerTest}
    echo "═══ Running all tests ═══"
    cd "$DEVENV_ROOT"
    # -ra adds pytest's "short test summary info" block listing every failure.
    # Tee the run so we can print a friendly pass/fail banner afterward, and
    # preserve pytest's own exit code (PIPESTATUS[0]) for CI.
    _out="$(mktemp)"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS -ra ${pytestDefaultMarkerArgs} src/tyo3/tests/ --cov=tyo3 --cov-report=term-missing "$@" 2>&1 | tee "$_out"
    _rc=''${PIPESTATUS[0]}
    echo ""
    if [ "$_rc" -eq 0 ]; then
      _passed="$(grep -oE '[0-9]+ passed' "$_out" | tail -1 | grep -oE '^[0-9]+')"
      echo "═══ ✅ ''${_passed:-0} of ''${_passed:-0} tests pass ═══"
    else
      echo "═══ ❌ Failures ═══"
      grep -E '^(FAILED|ERROR) ' "$_out" || true
      grep -E '^=+ .*(failed|error).* =+$' "$_out" | tail -1
    fi
    rm -f "$_out"
    exit "$_rc"
  '';

  scripts.test-quick.exec = ''
    ${detailPrelude}
    echo "═══ Running quick tests (no native extension needed) ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS ${pytestDefaultMarkerArgs} src/tyo3/tests/ \
      --ignore=src/tyo3/tests/test_rust_integration.py \
      --ignore=src/tyo3/tests/test_rust_snapshots.py \
      --ignore=src/tyo3/tests/test_coordinate_conversion.py \
      --ignore=src/tyo3/tests/test_property_based.py \
      --ignore=src/tyo3/tests/test_rust_performance.py "$@" 2>&1
  '';

  scripts.test-rust.exec = ''
    ${detailPrelude}
    echo "═══ Running Rust backend tests ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS ${pytestDefaultMarkerArgs} src/tyo3/tests/test_rust_integration.py src/tyo3/tests/test_rust_snapshots.py src/tyo3/tests/test_coordinate_conversion.py "$@" 2>&1
  '';

  scripts.test-property.exec = ''
    ${detailPrelude}
    echo "═══ Running property-based tests (Hypothesis) ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS ${pytestDefaultMarkerArgs} src/tyo3/tests/test_property_based.py "$@" 2>&1
  '';

  scripts.test-perf.exec = ''
    ${detailPrelude}
    echo "═══ Running performance benchmarks ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS ${pytestDefaultMarkerArgs} src/tyo3/tests/test_rust_performance.py "$@" 2>&1
  '';

  scripts.test-coverage.exec = ''
    ${detailPrelude}
    echo "═══ Running all tests with coverage report ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS ${pytestDefaultMarkerArgs} src/tyo3/tests/ --cov=tyo3 --cov-report=term-missing --cov-report=html "$@" 2>&1
    echo "═══ HTML coverage report: $DEVENV_ROOT/htmlcov/index.html ═══"
  '';

  scripts.test-ci.exec = ''
    ${detailPrelude}
    echo "═══ Running CI-style test suite ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS ${pytestDefaultMarkerArgs} src/tyo3/tests/ -x --cov=tyo3 --cov-report=term-missing "$@" 2>&1
  '';

  # ── Fast inner-loop scripts (for the async/snapshot refactor) ───────
  #
  # During the refactor you edit Rust constantly. A full `build` (maturin
  # develop) is ~17s and writes a 284MB .so; `cargo check` type-checks the
  # same code (catching every Ungil/Send/borrow error) in ~1s. Use `check-rust`
  # for the edit loop; only `build`/`rebuild` when you actually need to run
  # Python. See OPTION_C_SNAPSHOT_IMPLEMENTATION.md §3.

  scripts.check-rust.exec = ''
    echo "═══ cargo check (fast type-check — no build/link/install) ═══"
    cd "$DEVENV_ROOT/rust"
    cargo check "$@" 2>&1
  '';

  scripts.clippy.exec = ''
    echo "═══ cargo clippy (lint, -D warnings — matches CI gate) ═══"
    cd "$DEVENV_ROOT/rust"
    cargo clippy --all-targets -- -D warnings 2>&1
  '';

  scripts.rebuild.exec = ''
    echo "═══ Removing stale .so + rebuilding (avoids abi3/cpython shadowing) ═══"
    rm -f "$DEVENV_ROOT/src/tyo3/_native_impl"*.so
    cd "$DEVENV_ROOT"
    maturin develop 2>&1
    echo "═══ Rebuild complete ═══"
  '';

  # Pass-through pytest: `devenv shell -- pytest src/tyo3/tests/test_concurrency.py -v`
  # Lean by default; add `--detail` for the verbose debug flags.
  scripts.pytest.exec = ''
    ${detailPrelude}
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS "$@" 2>&1
  '';

  # Run an ad-hoc script with the package importable:
  #   `devenv shell -- pyrun .scratch/validate.py`
  scripts.pyrun.exec = ''
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python "$@" 2>&1
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
from tyo3 import TyO3Session
rp = TyO3Session('$DEVENV_ROOT/fixtures/simple_package')
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
    echo ""
    echo "  Tip: append --detail to any test script for verbose debug output"
    echo "       (DEBUG logs, locals, full tracebacks). Lean output is the default."
  '';

  # ── Shell entry / test ───────────────────────────────────

  enterShell = ''
    hello
    status
  '';

  enterTest = ''
    echo "Running CI-style tests..."
    cd "$DEVENV_ROOT"
    maturin develop 2>&1
    PYTHONPATH=src python -m pytest ${pytestLeanArgs} ${pytestDefaultMarkerArgs} src/tyo3/tests/ -x 2>&1
  '';
}
