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

  scripts.hello.exec = ''
    echo hello from $GREET
  '';

  enterShell = ''
    hello
    git --version
    rustc --version
    cargo --version
  '';

  enterTest = ''
    echo "Running tests"
    git --version | grep --color=auto "${pkgs.git.version}"
  '';
}
