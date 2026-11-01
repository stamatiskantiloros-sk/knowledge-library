#!/bin/bash
# Installs knowlib on an Apple Silicon Mac and creates the library. Safe to re-run.
#   bash ~/Documents/knowledge-library/scripts/install-mac.sh
# Library location: $KNOWLIB_LIBRARY, default ~/Documents/KnowledgeLibrary
set -uo pipefail
CODE="$(cd "$(dirname "$0")/.." && pwd)"
LIB="${KNOWLIB_LIBRARY:-$HOME/Documents/KnowledgeLibrary}"
mkdir -p "$LIB/.work"
LOG="$LIB/.work/install.log"
exec > >(tee "$LOG") 2>&1
step() { echo; echo "== $*"; }
fail() { echo; echo "INSTALL FAILED: $*"; echo "Log: $LOG"; exit 1; }

[ "$(uname -m)" = "arm64" ] || fail "needs an Apple Silicon Mac"
echo "knowlib install  $(date)  macOS $(sw_vers -productVersion)  code: $CODE  library: $LIB"

step "1/6 Homebrew"
command -v brew >/dev/null || { [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"; }
command -v brew >/dev/null || fail "Homebrew not found. Install it from https://brew.sh, then run this again."
brew --version | head -1

step "2/6 System tools: ffmpeg, pandoc, tesseract (+ Greek), uv"
for f in ffmpeg pandoc tesseract tesseract-lang uv; do
  if brew list --formula "$f" >/dev/null 2>&1; then echo "  ok $f"; else brew install "$f" || fail "brew install $f"; fi
done

step "3/6 Python environment ($CODE/.venv)"
cd "$CODE" || fail "cannot cd to $CODE"
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv || fail "uv venv"
uv pip install --python .venv/bin/python -e ".[mac,docs,dev]" || fail "pip install"
.venv/bin/python -c "import knowlib, mlx_whisper, mlx_vlm, ocrmac; print('  imports ok, knowlib', knowlib.__version__)" || fail "imports"

step "4/6 'knowlib' command"
mkdir -p "$HOME/.local/bin"
ln -sf "$CODE/.venv/bin/knowlib" "$HOME/.local/bin/knowlib"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) echo "  ok ~/.local/bin is on PATH" ;;
  *) grep -q 'knowlib: local bin' "$HOME/.zshrc" 2>/dev/null || \
       printf '\nexport PATH="$HOME/.local/bin:$PATH"  # knowlib: local bin\n' >> "$HOME/.zshrc"
     echo "  added ~/.local/bin to PATH in ~/.zshrc (new Terminal tabs will find 'knowlib')" ;;
esac
KL="$CODE/.venv/bin/knowlib"

step "5/6 Library at $LIB"
"$KL" init "$LIB" || fail "knowlib init"

step "6/6 Models: one-time download, ~8 GB, cached in ~/.cache/huggingface"
.venv/bin/python - <<'PY' || fail "model download"
from huggingface_hub import snapshot_download
for repo in ["mlx-community/whisper-large-v3-turbo", "mlx-community/Qwen3-VL-8B-Instruct-4bit"]:
    print("  ", repo, "->", snapshot_download(repo))
PY

step "Check"
"$KL" doctor -L "$LIB"
echo; echo "INSTALL OK  $(date)"
