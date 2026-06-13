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

  # ── tyo3.nvim curated plugin stack (Phase F: hermetic provisioning) ──────────
  #
  # The single-path UI specs (picker/ast_nav/sidebar) and the demos need the six
  # curated plugins + a python treesitter parser on the runtimepath. Provide them
  # from Nix, pinned to the *exact* revs in the dev box's `vim.pack` opt dir that
  # produced the working demos (GUIDE-phase-F §3.5) — so CI reproduces the recorded
  # behaviour. All six are pure-lua plugins built with `buildVimPlugin` from a
  # pinned `fetchFromGitHub`; the treesitter grammar is a separate `parser/*.so`
  # dir. The store paths are pure, so this is a stable CI contract.
  #
  # ⚠nvim-treesitter (+textobjects) are pinned to their **main**-branch revs (not
  # master) — the post-rewrite API the plugin's deps.lua targets. Don't swap these
  # for the drifting `pkgs.vimPlugins.*`; the pins are the source of truth.
  mkNvimPlugin = { name, owner, repo, rev, hash }:
    pkgs.vimUtils.buildVimPlugin {
      pname = name;
      version = "0-unstable-${builtins.substring 0 7 rev}";
      src = pkgs.fetchFromGitHub { inherit owner repo rev; sha256 = hash; };
      # buildVimPlugin's nativeCheckInputs `require()` every lua module at build
      # time (snacks/edgy/tiny-code-action/treesitter all have optional submodules
      # that pull deps absent during this isolated build, and LuaCATS `_meta` stubs
      # that error on require). We pin exact revs and load them at *runtime* where
      # their deps coexist, so the build-time sanity check adds nothing but
      # brittleness — `doCheck = false` drops the require + command check hooks
      # (stdenv only adds nativeCheckInputs when doCheck is set).
      doCheck = false;
    };
  tyo3NvimPlugins = [
    (mkNvimPlugin {
      name = "snacks.nvim"; owner = "folke"; repo = "snacks.nvim";
      rev = "e6fd58c82f2f3fcddd3fe81703d47d6d48fc7b9f";
      hash = "06v4v63xc818bc4csj49ri30my24hmpddhr2a2452q7jm10ijaim";
    })
    (mkNvimPlugin {
      name = "edgy.nvim"; owner = "folke"; repo = "edgy.nvim";
      rev = "ebb77fde6f5cb2745431c6c0fe57024f66471728";
      hash = "1psavlldajgfvwx0jjhwdilccrhz38p880jsrddmrmfx9yq3yl5s";
    })
    (mkNvimPlugin {
      name = "tiny-code-action.nvim"; owner = "rachartier"; repo = "tiny-code-action.nvim";
      rev = "0d040ed81f7953118b81cd12681fcdfcac069803";
      hash = "186d7zyrcb7n2bmqndy434jkl69ffrkwnc61v1nkgfjlxrw76psh";
    })
    (mkNvimPlugin {
      name = "treewalker.nvim"; owner = "aaronik"; repo = "treewalker.nvim";
      rev = "0b081bf6c6875cf3e478b633796a9e2b64b730e8";
      hash = "14albx393qhsm7nckrlrm5a9hdasmx7wv5kk5msjpxvldm120g7r";
    })
    (mkNvimPlugin {
      name = "nvim-treesitter"; owner = "nvim-treesitter"; repo = "nvim-treesitter";
      rev = "4916d6592ede8c07973490d9322f187e07dfefac";
      hash = "0wgwbxi6h99fsp901xysm0424lhgrh9fq1nlck02m53qbfs7l11x";
    })
    (mkNvimPlugin {
      name = "nvim-treesitter-textobjects"; owner = "nvim-treesitter"; repo = "nvim-treesitter-textobjects";
      rev = "851e865342e5a4cb1ae23d31caf6e991e1c99f1e";
      hash = "03dbmmc1s63ygm11mn27sx3bg43ygcy12c40kdbc3giha8953skw";
    })
  ];
  # A runtimepath dir carrying the python parser as `parser/python.so` — the file
  # `vim.treesitter`/textobjects resolve for a python buffer (the AST specs +
  # context.lua need it; a pristine `--clean` nvim bundles only c/lua/vim/markdown).
  tyo3NvimPyGrammar = pkgs.runCommand "tyo3-nvim-ts-python-grammar" { } ''
    mkdir -p $out/parser
    ln -s ${pkgs.tree-sitter-grammars.tree-sitter-python}/parser $out/parser/python.so
  '';
  # The CI contract: `:`-joined plugin dirs + the grammar dir. bootstrap.lua
  # (specs) and pack.lua (demos) both read it; the pristine headless nvim is the
  # Nix-built neovim-unwrapped (never the PATH wrapper — see the demo setup.sh note).
  tyo3NvimDeps = lib.concatStringsSep ":" (map toString tyo3NvimPlugins ++ [ (toString tyo3NvimPyGrammar) ]);
  tyo3NvimBin = "${pkgs.neovim-unwrapped}/bin/nvim";
  # Re-encode a vhs webm output (path in $WEBM) to a sane size: vhs emits a huge
  # default bitrate; VP9 CRF 36 is ~70% smaller and visually identical for a
  # screencast (e.g. showcase 2.5M → ~800K). No-op if the file/ffmpeg is missing.
  reencodeWebm = ''
    if [ -f "$WEBM" ] && command -v ffmpeg >/dev/null 2>&1; then
      echo "── re-encoding $WEBM (VP9 crf40 @12fps) ──"
      ffmpeg -y -hide_banner -loglevel error -i "$WEBM" -r 12 -c:v libvpx-vp9 -crf 40 -b:v 0 \
        -deadline good -cpu-used 2 -row-mt 1 -an "$WEBM.tmp.webm" \
        && mv "$WEBM.tmp.webm" "$WEBM"
    fi
  '';
in
{
  env.GREET = "devenv";

  # tyo3.nvim hermetic plugin stack (Phase F). Exported in every devenv shell so
  # the Lua UI specs (via tests/bootstrap.lua) and the demos (via demo/pack.lua)
  # resolve the pinned curated plugins + python grammar from the Nix store. Unset
  # outside devenv, where both consumers fall back to the local vim.pack opt dir.
  env.TYO3_NVIM_DEPS = tyo3NvimDeps;

  packages = [
    pkgs.git
    pkgs.uv
    pkgs.maturin         # Build Python extensions with PyO3
    pkgs.mold            # Fast linker (replaces GNU ld for Rust LTO builds)
    pkgs.sccache         # Compiler cache for Rust (survives cargo clean)
    pkgs.vhs             # Scripted terminal recordings (tyo3.nvim demo: tour.tape → gif/cast)
    pkgs.ffmpeg          # Re-encode vhs webm outputs to a sane size (demo-record post-step)
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
    _ci_rc=$?
    [ "$_ci_rc" -eq 0 ] || exit "$_ci_rc"
    echo ""
    echo "═══ Running tyo3.nvim Lua spec suite (test-nvim) ═══"
    test-nvim
  '';

  # test-nvim: the tyo3.nvim Lua spec gate. Provisions the curated stack
  # hermetically (TYO3_NVIM_DEPS, exported above from the pinned Nix plugins) and
  # runs every headless spec — engine specs (dep-free) + the UI specs
  # (picker/ast_nav/sidebar), which now find the stack instead of skipping. Fails
  # non-zero on any spec failure. Folded into test-ci above.
  #
  # The pristine nvim is the Nix-built neovim-unwrapped (never the PATH wrapper,
  # which injects user config even under --clean — see demo/setup.sh + the
  # nvim-demo-pristine-binary note). bootstrap.lua puts TYO3_NVIM_DEPS on the rtp.
  scripts.test-nvim.exec = ''
    echo "═══ tyo3.nvim Lua spec suite (hermetic) ═══"
    cd "$DEVENV_ROOT"
    # The specs spawn the daemon (`python -m tyo3.daemon`), which imports the
    # native extension — make sure it's built.
    if ! ls src/tyo3/_native_impl*.so >/dev/null 2>&1; then
      echo "── native extension missing; building (maturin develop) ──"
      maturin develop 2>&1
    fi
    export PYTHONPATH="$DEVENV_ROOT/src''${PYTHONPATH:+:$PYTHONPATH}"
    NVIM="${tyo3NvimBin}"
    if [ ! -x "$NVIM" ]; then
      echo "❌ pristine nvim not found at $NVIM"; exit 1
    fi
    echo "nvim:  $NVIM ($("$NVIM" --version | head -1))"
    if [ -z "''${TYO3_NVIM_DEPS:-}" ]; then
      echo "⚠ TYO3_NVIM_DEPS unset — UI specs will skip (run inside the devenv shell)."
    else
      echo "deps:  $TYO3_NVIM_DEPS"
    fi
    # Engine specs (dep-free) + UI specs (provisioned). picker has no headless
    # spec (demo-only); ast_nav/sidebar carry the headless UI assertions.
    specs="smoke context lsp lsp_nav lsp_codeaction review_dedup lsp_symbols ast_nav sidebar hub"
    _failed=0
    _ran=0
    for t in $specs; do
      _ran=$((_ran + 1))
      echo ""
      echo "── spec: $t ──"
      _out="$(mktemp)"
      timeout 240 "$NVIM" --headless --clean \
        -u editors/tyo3.nvim/tests/minimal_init.lua \
        -c "luafile editors/tyo3.nvim/tests/$t.lua" 2>&1 | tee "$_out"
      _rc=''${PIPESTATUS[0]}
      # Each spec `cquit 1`s on failure / `qall!`s clean, so the exit code is the
      # primary signal; cross-check the spec's own "N failed" summary line.
      _nfail="$(grep -oE '[0-9]+ failed' "$_out" | tail -1 | grep -oE '^[0-9]+')"
      rm -f "$_out"
      if [ "$_rc" -ne 0 ] || [ "''${_nfail:-0}" -ne 0 ]; then
        echo "[SPEC FAIL] $t (exit=$_rc, failed=''${_nfail:-?})"
        _failed=$((_failed + 1))
      else
        echo "[SPEC PASS] $t"
      fi
    done
    echo ""
    if [ "$_failed" -eq 0 ]; then
      echo "═══ ✅ tyo3.nvim specs: $_ran ran, 0 failed ═══"
    else
      echo "═══ ❌ tyo3.nvim specs: $_ran ran, $_failed failed ═══"
      exit 1
    fi
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
  # Render the short, looping HERO demo (the README advertisement): money shot
  # + one LSP wow + the Phase-2 review-ack code action, sidebar on throughout.
  scripts.demo-record-hero.exec = ''
    echo "═══ Recording tyo3.nvim HERO demo (vhs) ═══"
    cd "$DEVENV_ROOT"
    if ! command -v vhs >/dev/null 2>&1; then
      echo "vhs not found on PATH — is the devenv shell active?"
      exit 1
    fi
    vhs editors/tyo3.nvim/demo/hero/hero.tape
    WEBM=editors/tyo3.nvim/demo/hero/hero.webm
    ${reencodeWebm}
    echo "═══ Wrote editors/tyo3.nvim/demo/hero/hero.gif ═══"
  '';

  # Render the HUB demo (proj 28): every TyO3 view reached from one keyboard
  # surface — the hub (`<leader>t`) fuzzy-filters to Entities / Authored /
  # Affected and opens that snacks picker; notes are authored from the act
  # surface (`<leader>c`). No `:TyO3*` is typed (snacks resolves from the store).
  scripts.demo-record-picker.exec = ''
    echo "═══ Recording tyo3.nvim PICKER demo (vhs) ═══"
    cd "$DEVENV_ROOT"
    if ! command -v vhs >/dev/null 2>&1; then
      echo "vhs not found on PATH — is the devenv shell active?"
      exit 1
    fi
    vhs editors/tyo3.nvim/demo/picker/picker.tape
    WEBM=editors/tyo3.nvim/demo/picker/picker.webm
    ${reencodeWebm}
    echo "═══ Wrote editors/tyo3.nvim/demo/picker/picker.gif ═══"
  '';

  # Render the CODE ACTION demo (proj 28, Phase C): the single "act on the entity"
  # surface — `gra` opens the tiny-code-action buffer picker over the registry
  # providers (Author / Write doc / Move / Explain / Simplify). tiny-code-action +
  # snacks are resolved from the local vim.pack opt checkout by the codeaction
  # init.lua (demo/pack.lua); hermetic CI provisioning is the Phase-F task.
  scripts.demo-record-codeaction.exec = ''
    echo "═══ Recording tyo3.nvim CODE ACTION demo (vhs) ═══"
    cd "$DEVENV_ROOT"
    if ! command -v vhs >/dev/null 2>&1; then
      echo "vhs not found on PATH — is the devenv shell active?"
      exit 1
    fi
    vhs editors/tyo3.nvim/demo/codeaction/codeaction.tape
    WEBM=editors/tyo3.nvim/demo/codeaction/codeaction.webm
    ${reencodeWebm}
    echo "═══ Wrote editors/tyo3.nvim/demo/codeaction/codeaction.gif ═══"
  '';

  # Render the AST NAV demo (proj 28, Phase D): treewalker motion + textobject
  # selection — the dock tracks the syntax tree, a textobject feeds the act path.
  # nvim-treesitter(+textobjects)/treewalker/snacks are resolved from the local
  # vim.pack opt checkout, and the python parser from the nix-store grammars, by
  # the astnav init.lua (demo/pack.lua). Hermetic CI provisioning is Phase F.
  scripts.demo-record-astnav.exec = ''
    echo "═══ Recording tyo3.nvim AST NAV demo (vhs) ═══"
    cd "$DEVENV_ROOT"
    if ! command -v vhs >/dev/null 2>&1; then
      echo "vhs not found on PATH — is the devenv shell active?"
      exit 1
    fi
    vhs editors/tyo3.nvim/demo/astnav/astnav.tape
    WEBM=editors/tyo3.nvim/demo/astnav/astnav.webm
    ${reencodeWebm}
    echo "═══ Wrote editors/tyo3.nvim/demo/astnav/astnav.gif ═══"
  '';

  # Render the edgy accordion sidebar demo (Phase E).
  scripts.demo-record-sidebar.exec = ''
    echo "═══ Recording tyo3.nvim EDGY SIDEBAR demo (vhs) ═══"
    cd "$DEVENV_ROOT"
    if ! command -v vhs >/dev/null 2>&1; then
      echo "vhs not found on PATH — is the devenv shell active?"
      exit 1
    fi
    vhs editors/tyo3.nvim/demo/sidebar/sidebar.tape
    WEBM=editors/tyo3.nvim/demo/sidebar/sidebar.webm
    ${reencodeWebm}
    echo "═══ Wrote editors/tyo3.nvim/demo/sidebar/sidebar.gif ═══"
  '';

  # Render the long, wow-factor SHOWCASE tour (proj 28): kinetic AST navigation,
  # the full `gra` action hub, durable identity (note + doc ride a move), the live
  # affected-set/blast-radius, and the snacks diagnostics picker + review-ack —
  # the sidebar narrating with every pane lit. The curated stack is resolved
  # hermetically (TYO3_NVIM_DEPS) or from the local vim.pack opt dir by the
  # showcase init.lua (demo/pack.lua).
  scripts.demo-record-showcase.exec = ''
    echo "═══ Recording tyo3.nvim SHOWCASE demo (vhs) ═══"
    cd "$DEVENV_ROOT"
    if ! command -v vhs >/dev/null 2>&1; then
      echo "vhs not found on PATH — is the devenv shell active?"
      exit 1
    fi
    vhs editors/tyo3.nvim/demo/showcase/showcase.tape
    WEBM=editors/tyo3.nvim/demo/showcase/showcase.webm
    ${reencodeWebm}
    echo "═══ Wrote editors/tyo3.nvim/demo/showcase/showcase.gif ═══"
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
