#!/bin/sh
# Commons one-line installer (macOS / Linux):
#   curl -fsSL https://raw.githubusercontent.com/Neoo-Blue/commons-memory/main/install.sh | sh
# Options (env): COMMONS_VERSION=v0.7.1  COMMONS_EXTRAS=http,embed (comma list or "all")  COMMONS_NO_INIT=1
#                COMMONS_INSTALLER=uv|pipx|pip (force a method)
# Installs the `commons` CLI (PyPI, else the GitHub release wheel, else git), runs `commons init`, prints next steps.
set -eu
REPO="Neoo-Blue/commons-memory"
EXTRAS="${COMMONS_EXTRAS:-http,embed}"
say() { printf '\033[1;35mcommons:\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mcommons: error:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
fetch() { if have curl; then curl -fsSL "$1"; elif have wget; then wget -qO- "$1"; else die "need curl or wget"; fi; }

VER="${COMMONS_VERSION:-}"
if [ -z "$VER" ]; then
  VER=$(fetch "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1) || true
fi
# Sources, tried in order until one installs: PyPI, the release's wheel, the release tag from git.
if [ -n "$VER" ]; then
  N=${VER#v}
  SPECS="commons-memory[$EXTRAS]==$N|commons-memory[$EXTRAS] @ https://github.com/$REPO/releases/download/$VER/commons_memory-$N-py3-none-any.whl|commons-memory[$EXTRAS] @ git+https://github.com/$REPO@$VER"
else
  say "could not resolve the latest release (rate-limited?); trying PyPI, then main"
  SPECS="commons-memory[$EXTRAS]|commons-memory[$EXTRAS] @ git+https://github.com/$REPO"
fi

PY=""
for c in python3 python; do
  if have "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY=$c; break; fi
done

METHOD="${COMMONS_INSTALLER:-}"
if [ -z "$METHOD" ]; then
  if have uv; then METHOD=uv; elif have pipx; then METHOD=pipx; elif [ -n "$PY" ]; then METHOD=pip; else METHOD=uv-bootstrap; fi
fi
say "installing ${VER:-the latest} via $METHOD"
PYARG=""
case "$METHOD" in
  uv-bootstrap)
    say "no uv/pipx/Python 3.10+ found; installing uv (https://astral.sh/uv)"
    fetch https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; export PATH
    METHOD=uv; PYARG="--python 3.12" ;;
  uv|pipx) ;;
  pip)
    VENV="${COMMONS_VENV:-$HOME/.commons/venv}"
    "$PY" -m venv "$VENV" || die "python venv failed (Debian/Ubuntu: apt install python3-venv)"
    "$VENV/bin/python" -m pip install -q --upgrade pip ;;
  *) die "unknown COMMONS_INSTALLER=$METHOD" ;;
esac
install_spec() {
  case "$METHOD" in
    uv)   uv tool install --force $PYARG "$1" ;;
    pipx) pipx install --force "$1" ;;
    pip)  "$VENV/bin/python" -m pip install -q "$1" && mkdir -p "$HOME/.local/bin" \
            && ln -sf "$VENV/bin/commons" "$HOME/.local/bin/commons" ;;
  esac
}
OK=""; OLDIFS=$IFS; IFS='|'
for SPEC in $SPECS; do
  IFS=$OLDIFS
  if install_spec "$SPEC"; then OK=1; break; fi
  say "not from ${SPEC#*@ }; trying the next source"
  IFS='|'
done
IFS=$OLDIFS
[ -n "$OK" ] || die "could not install commons-memory from PyPI, the release wheel or git"

PATH="$HOME/.local/bin:$PATH"; export PATH
have commons || die "installed, but 'commons' is not on PATH; add ~/.local/bin to PATH"
say "installed $(commons --version)"
if [ "${COMMONS_NO_INIT:-0}" != "1" ]; then commons init >/dev/null && say "database ready: ~/.commons/commons.db"; fi
cat <<'NEXT'

Next steps
  1. Connect an AI:      commons connect claude --platform claude-code     (or cursor, codex, gemini, generic)
  2. Try it:             commons remember "Commons is installed" && commons search "installed"
  3. Dashboard:          commons dashboard            -> http://127.0.0.1:8766
  4. Team / other boxes: commons serve --transport http --host <tailnet-ip> --port 8770
  5. Paste PROMPT.md into any AI so it follows the Commons routine:
     https://github.com/Neoo-Blue/commons-memory/blob/main/PROMPT.md
If 'commons' isn't found in a new shell, add ~/.local/bin to your PATH.
NEXT
