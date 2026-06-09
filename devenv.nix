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
    pkgs.mold            # Fast linker (replaces GNU ld for Rust LTO builds)
    pkgs.sccache         # Compiler cache for Rust (survives cargo clean)
    pkgs.vhs             # Scripted terminal recordings (tyo3.nvim demo: tour.tape → gif/cast)
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
    echo "═══ Rust: cargo test --all-targets ═══"
    cargo test --manifest-path rust/Cargo.toml --all-targets
    _rust_rc=$?
    echo ""
    echo "═══ Python: pytest ═══"
    # -ra adds pytest's "short test summary info" block listing every failure.
    # Tee the run so we can print a friendly pass/fail banner afterward, and
    # preserve pytest's own exit code (PIPESTATUS[0]) for CI.
    _out="$(mktemp)"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS -ra ${pytestDefaultMarkerArgs} src/tyo3/tests/ --cov=tyo3 --cov-report=term-missing "$@" 2>&1 | tee "$_out"
    _pytest_rc=''${PIPESTATUS[0]}
    echo ""
    if [ "$_rust_rc" -eq 0 ] && [ "$_pytest_rc" -eq 0 ]; then
      _passed="$(grep -oE '[0-9]+ passed' "$_out" | tail -1 | grep -oE '^[0-9]+')"
      echo "═══ ✅ Rust and Python tests pass; pytest: ''${_passed:-0} passed ═══"
    else
      echo "═══ ❌ Failures ═══"
      if [ "$_rust_rc" -ne 0 ]; then
        echo "Rust cargo tests failed with exit code $_rust_rc"
      fi
      if [ "$_pytest_rc" -ne 0 ]; then
        grep -E '^(FAILED|ERROR) ' "$_out" || true
        grep -E '^=+ .*(failed|error).* =+$' "$_out" | tail -1
      fi
    fi
    rm -f "$_out"
    if [ "$_rust_rc" -ne 0 ]; then
      exit "$_rust_rc"
    fi
    exit "$_pytest_rc"
  '';

  # test-fast: no-coverage parallel runner for the inner dev loop.
  #   devenv shell -- test-fast                     # all tests (default)
  #   devenv shell -- test-fast --all               # all tests (explicit)
  #   devenv shell -- test-fast test_project.py     # only that file (bare
  #                                                 #   names are resolved
  #                                                 #   under src/tyo3/tests/)
  #   devenv shell -- test-fast test_project.py::test_open  # a single node id
  #   devenv shell -- test-fast src/tyo3/tests/foo.py -k bar  # path + flags
  # Any arg that looks like a test selector (a path, a *.py file, or a ::node
  # id) narrows the run to just those targets; everything else (flags, -k
  # exprs) passes through to pytest. With no selector, the full suite runs.
  scripts.test-fast.exec = ''
    ${detailPreludePerTest}
    cd "$DEVENV_ROOT"
    _force_all=0
    _targets=()
    _passthru=()
    for _arg in "$@"; do
      case "$_arg" in
        --all)          _force_all=1 ;;
        -*)             _passthru+=("$_arg") ;;          # flag — pass through
        */*)            _targets+=("$_arg") ;;           # explicit path — verbatim
        *.py|*.py::*|*::*) _targets+=("src/tyo3/tests/$_arg") ;;  # bare file/node id
        *)              _passthru+=("$_arg") ;;          # e.g. value of -k
      esac
    done
    if [ "$_force_all" -eq 1 ] || [ ''${#_targets[@]} -eq 0 ]; then
      _targets=(src/tyo3/tests/)
      _xdist=(-n 4 --dist loadscope)                    # full suite — parallel
      echo "═══ Running all tests (no coverage, parallel) ═══"
    else
      _xdist=(-n 0)                                      # selected — no xdist overhead
      echo "═══ Running selected tests: ''${_targets[*]} (no coverage, serial) ═══"
    fi
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS ${pytestDefaultMarkerArgs} "''${_targets[@]}" "''${_xdist[@]}" --durations=0 "''${_passthru[@]}" 2>&1
  '';

  # scripts.test-quick is retired — test-fast covers the fast dev loop now.

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
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS ${pytestDefaultMarkerArgs} src/tyo3/tests/ --cov=tyo3 --cov-report=term-missing --cov-report=html -n auto "$@" 2>&1
    echo "═══ HTML coverage report: $DEVENV_ROOT/htmlcov/index.html ═══"
  '';

  scripts.test-ci.exec = ''
    ${detailPrelude}
    echo "═══ Running CI-style test suite ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS ${pytestDefaultMarkerArgs} src/tyo3/tests/ -x --cov=tyo3 --cov-report=term-missing "$@" 2>&1
  '';

  # ── Spine refactor scripts (REFINED_IMPLEMENTATION_PLAN, Phase 0+) ──
  #
  # The refined refactor lands as a phased series. Phase 0 writes the target
  # contracts up front as `test_final_*.py` invariant tests (failing or
  # xfail-strict on today's code, each tagged with the phase that turns it
  # green) plus a parity-oracle harness. `test-final` is the single gate to
  # run as each phase lands; `parity-oracle` runs the harness self-test that
  # guards the Phase 2–4 graph cutover. See
  # .scratch/projects/15-implementation-plan/REFINED_IMPLEMENTATION_PLAN.md.

  scripts.test-final.exec = ''
    ${detailPreludePerTest}
    echo "═══ Spine refactor — final invariant suite (Phase 0 contracts) ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS -ra --no-cov \
      -p no:cacheprovider \
      src/tyo3/tests/test_final_content_spine.py \
      src/tyo3/tests/test_final_no_read_side_writes.py \
      src/tyo3/tests/test_final_commit_delta_contract.py \
      src/tyo3/tests/test_final_transaction_rollback.py \
      src/tyo3/tests/test_final_bus_contract.py \
      src/tyo3/tests/test_final_derived_contract.py \
      src/tyo3/tests/test_final_hash_ast.py \
      src/tyo3/tests/test_final_parity_oracle.py \
      src/tyo3/tests/test_final_acceptance.py \
      "$@" 2>&1
  '';

  scripts.parity-oracle.exec = ''
    ${detailPreludePerTest}
    echo "═══ Spine refactor — parity oracle harness (Phase 0.8) ═══"
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest $PYTEST_LOG_ARGS -ra --no-cov \
      -p no:cacheprovider \
      src/tyo3/tests/test_final_parity_oracle.py \
      "$@" 2>&1
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

  # ── Neovim plugin demo ───────────────────────────────────────
  #
  # Render the scripted tyo3.nvim demos to a GIF + asciinema cast via vhs. The
  # tapes (editors/tyo3.nvim/demo/<name>/*.tape) drive a real terminal nvim
  # through the plugin's verbs; setup.sh builds the synthetic shop project fresh.
  # CI-runnable (no display). See .scratch/projects/20-neovim-integration/DEMO_RECORDING.md.
  scripts.demo-record.exec = ''
    echo "═══ Recording tyo3.nvim demo (vhs) ═══"
    cd "$DEVENV_ROOT"
    if ! command -v vhs >/dev/null 2>&1; then
      echo "vhs not found on PATH — is the devenv shell active?"
      exit 1
    fi
    vhs editors/tyo3.nvim/demo/default/tour.tape
    echo "── GIF done; recording asciinema cast ──"
    # vhs 0.11 does not emit asciinema .cast natively, so the cast is recorded
    # by a small dependency-free PTY driver running the same scripted scenes.
    python editors/tyo3.nvim/demo/default/record_cast.py editors/tyo3.nvim/demo/default/tour.cast || true
    echo "═══ Wrote editors/tyo3.nvim/demo/default/tour.gif + tour.cast ═══"
  '';

  # Render the comprehensive cursor-CONTEXT demo (context = "cursor").
  scripts.demo-record-context.exec = ''
    echo "═══ Recording tyo3.nvim CONTEXT demo (vhs) ═══"
    cd "$DEVENV_ROOT"
    if ! command -v vhs >/dev/null 2>&1; then
      echo "vhs not found on PATH — is the devenv shell active?"
      exit 1
    fi
    vhs editors/tyo3.nvim/demo/context/context.tape
    echo "═══ Wrote editors/tyo3.nvim/demo/context/context.gif ═══"
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
