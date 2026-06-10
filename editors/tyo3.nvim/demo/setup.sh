#!/usr/bin/env bash
# tyo3.nvim demo — build the synthetic "shop" project and export its path.
#
# Source me (the tour.tape does `source setup.sh && cd $TYO3_DEMO_DIR`). Reuses
# the exact project from `src/tyo3/demo/tour.py` so the recording is determ-
# inistic and matches the CLI tour. Pre-creates an empty-but-importing
# checkout.py so the Scene-4 atomic move binds as a *Moved* (identity preserved).
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # editors/tyo3.nvim/demo
_plugin="$(cd "${_here}/.." && pwd)"                         # editors/tyo3.nvim

_base="$(mktemp -d -t tyo3-demo-XXXXXX)"
python - "${_base}" <<'PY'
import sys
from pathlib import Path
from tyo3.demo.tour import _build_project

proj = Path(sys.argv[1]) / "shop"
_build_project(proj)
# A known, pre-existing project file with checkout's imports → the move binds.
(proj / "checkout.py").write_text("from book import Book\nfrom money import usd\n")
PY

export TYO3_DEMO_DIR="${_base}/shop"
export TYO3_PLUGIN_DIR="${_plugin}"

# The `explain` code action's LLM seam, set to the curated *offline* backend so
# the recording shows model-quality prose deterministically — no network, no key
# (src/tyo3/demo/explanations.py). The daemon, spawned by nvim, inherits this.
# A real install sets `TYO3_LLM=anthropic` + ANTHROPIC_API_KEY instead.
export TYO3_LLM=callable
export TYO3_LLM_CALLABLE=tyo3.demo.explanations:explain

# Resolve a *pristine* nvim for the recording. The `nvim` on PATH here is a
# Nix/home-manager wrapper that force-injects the user config dir (~/.dotfiles/
# nvim and its after/ftplugin) onto the runtimepath EVEN under `--clean` and
# regardless of XDG overrides — that after/ftplugin/python.lua requires
# which-key (absent in this isolated env) and erupts at startup, overwriting
# the recording with an E5113 traceback. The wrapper is a chain of bash scripts
# that finally `exec` the real ELF `neovim-unwrapped` binary, which honours
# `--clean` and loads only $VIMRUNTIME. Follow the exec chain to that ELF (no
# hardcoded store hash) and drive the demo with it directly.
_resolve_nvim() {
  local bin next guard=0
  bin="$(command -v nvim)" || return 1
  bin="$(readlink -f "${bin}")"
  while [ "${guard}" -lt 10 ]; do
    guard=$((guard + 1))
    if file -b "${bin}" 2>/dev/null | grep -q ELF; then
      printf '%s\n' "${bin}"
      return 0
    fi
    next="$(grep -oE 'exec (-a [^ ]+ )?"?/nix/store/[^ "]+/bin/nvim' "${bin}" 2>/dev/null \
            | grep -oE '/nix/store/[^ "]+/bin/nvim' | head -1)"
    [ -z "${next}" ] && break
    bin="${next}"
  done
  printf '%s\n' "${bin}"
}
export TYO3_NVIM="$(_resolve_nvim)"

echo "tyo3 demo project ready: ${TYO3_DEMO_DIR}"
echo "tyo3 demo nvim:          ${TYO3_NVIM}"
