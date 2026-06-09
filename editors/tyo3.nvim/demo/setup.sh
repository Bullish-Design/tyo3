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

# Fully isolate Neovim from any ambient user config. `--clean` alone is not
# enough here (a user config under $XDG_CONFIG_HOME/nvim — often symlinked from
# a dotfiles repo — can still load its after/ftplugin and break the recording),
# so point all XDG bases at fresh, empty dirs. Combined with `nvim --clean -u
# demo/init.lua`, nvim then loads *only* $VIMRUNTIME + tyo3.nvim.
export XDG_CONFIG_HOME="${_base}/xdg/config"
export XDG_DATA_HOME="${_base}/xdg/data"
export XDG_STATE_HOME="${_base}/xdg/state"
export XDG_CACHE_HOME="${_base}/xdg/cache"
mkdir -p "${XDG_CONFIG_HOME}" "${XDG_DATA_HOME}" "${XDG_STATE_HOME}" "${XDG_CACHE_HOME}"

echo "tyo3 demo project ready: ${TYO3_DEMO_DIR}"
