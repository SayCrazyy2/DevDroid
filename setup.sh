#!/usr/bin/env bash
# DevDroid setup.sh — robust Termux/Linux installer
# - Termux: pkg + pip + rish
# - Linux/macOS: apt/brew + pipx/venv fallback
# Usage: bash setup.sh [--update] [--uninstall] [--help]
set -euo pipefail

VERSION="2.2.6"
REPO="SayCrazyy2/DevDroid"
RAW="https://raw.githubusercontent.com/$REPO/main"

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; CYAN='\033[36m'; BOLD='\033[1m'; RESET='\033[0m'
log(){ echo -e "${GREEN}[✓]${RESET} $*"; }
info(){ echo -e "${CYAN}[·]${RESET} $*"; }
warn(){ echo -e "${YELLOW}[!]${RESET} $*"; }
err(){ echo -e "${RED}[!]${RESET} $*" >&2; }

usage(){
  cat <<EOF
DevDroid setup $VERSION
Usage: bash setup.sh [options]
  --update      Force re-download devtools.py
  --uninstall   Remove installed files
  --check       Only check env (adb, python, rish)
  --help        This help
EOF
}

ACTION="install"
for a in "${@:-}"; do case "$a" in --update) ACTION="update";; --uninstall) ACTION="uninstall";; --check) ACTION="check";; --help|-h) usage; exit 0;; *) warn "unknown arg $a";; esac; done

is_termux(){ [[ -n "${PREFIX:-}" && -d "${PREFIX:-}" ]]; }
have(){ command -v "$1" >/dev/null 2>&1; }

do_check(){
  echo -e "${BOLD}== Env check ==${RESET}"
  echo "Termux: $(is_termux && echo yes || echo no)  PREFIX=${PREFIX:-n/a}"
  echo -n "python: "; python3 --version 2>&1 || echo "missing"
  echo -n "pip: "; pip --version 2>&1 | head -1 || pip3 --version 2>&1 | head -1 || echo "missing"
  echo -n "adb: "; adb --version 2>&1 | head -1 || echo "missing (pkg install android-tools)"
  echo -n "rish: "; rish -c 'id' 2>&1 | head -1 || echo "missing (Shizuku -> Export files)"
  echo -n "shizuku dex: "; ls -lh /data/local/tmp/shizuku/rish* 2>&1 | head -5 || echo "not found"
  echo -n "a/websockets: "; python3 -c "import aiohttp,websockets;print(aiohttp.__version__, websockets.__version__)" 2>&1 || echo "missing (pip install aiohttp websockets)"
  have termux-wake-lock && echo "wake-lock: yes" || echo "wake-lock: available via pkg install termux-tools"
}

if [[ "$ACTION" == "check" ]]; then do_check; exit 0; fi

if [[ "$ACTION" == "uninstall" ]]; then
  warn "Removing devtools.py requirements..."
  rm -f devtools.py requirements.txt 2>/dev/null || true
  if is_termux && [[ -f "$PREFIX/bin/devtools" ]]; then rm -f "$PREFIX/bin/devtools"; log "removed $PREFIX/bin/devtools"; fi
  log "Uninstall done. (pip packages kept: pip uninstall aiohttp websockets to remove)"
  exit 0
fi

echo -e "${BOLD}== DevDroid setup v$VERSION ==${RESET}"

# --- packages ---
if is_termux; then
  info "Termux detected (PREFIX=$PREFIX)"
  # Use stable mirrors; don't fail on pkg up
  pkg update -y 2>&1 | tail -5 || true
  pkg install -y python android-tools termux-tools 2>&1 | tail -10 || pkg install -y python android-tools 2>&1 | tail -10
  PIP="pip"
  # optional: termux-wake-lock permission
  have termux-wake-lock || warn "tip: pkg install termux-tools enables wake-lock (prevents kill in background)"
else
  info "Generic Linux/macOS"
  if have apt; then
    if [[ $EUID -eq 0 ]]; then apt update && apt install -y python3 python3-pip android-tools-adb 2>/dev/null || apt install -y python3 python3-pip adb || true
    else sudo apt update && sudo apt install -y python3 python3-pip android-tools-adb 2>/dev/null || sudo apt install -y python3 python3-pip adb || true
    fi
  elif have brew; then brew install python android-platform-tools 2>&1 | tail -5 || true
  elif have dnf; then sudo dnf install -y python3 python3-pip android-tools 2>&1 | tail -5 || true
  elif have pacman; then sudo pacman -Sy --noconfirm python python-pip android-tools 2>&1 | tail -5 || true
  fi
  PIP="pip3"
  have pip3 || PIP="pip"
fi

# --- python deps (prefer --break-system-packages on Termux/new pip) ---
info "Installing Python deps: aiohttp websockets"
if $PIP install --break-system-packages -q aiohttp websockets 2>&1 | tail -5; then true
elif $PIP install -q aiohttp websockets 2>&1 | tail -5; then true
elif python3 -m pip install --break-system-packages -q aiohttp websockets 2>&1 | tail -5; then true
else warn "pip install failed — try manually: pip install aiohttp websockets"; fi

# verify
if python3 -c "import aiohttp,websockets" 2>&1; then log "Python deps OK ($(python3 -c 'import aiohttp,websockets;print(aiohttp.__version__, websockets.__version__)'))"; else warn "Python deps still missing"; fi

# --- fetch devtools.py ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo ".")"
TARGET="$SCRIPT_DIR/devtools.py"
if [[ ! -f "$TARGET" || "$ACTION" == "update" ]]; then
  info "Fetching devtools.py → $TARGET"
  if have curl; then curl -fsSL -o "$TARGET" "$RAW/devtools.py" || curl -o "$TARGET" "$RAW/devtools.py"
  elif have wget; then wget -q -O "$TARGET" "$RAW/devtools.py" || wget -O "$TARGET" "$RAW/devtools.py"
  else err "Need curl or wget to fetch devtools.py"; exit 1
  fi
  chmod +x "$TARGET"
  log "Fetched $(wc -l < "$TARGET") lines"
else
  info "devtools.py already at $TARGET ($(wc -l < "$TARGET") lines)"
  chmod +x "$TARGET" 2>/dev/null || true
fi

# also ensure requirements.txt / pyproject
if [[ ! -f "$SCRIPT_DIR/requirements.txt" ]]; then
  echo -e "aiohttp>=3.9\nwebsockets>=12" > "$SCRIPT_DIR/requirements.txt"
fi

# --- Termux bin wrapper ---
if is_termux; then
  WRAP="$PREFIX/bin/devtools"
  cat > "$WRAP" <<EOS
#!/data/data/com.termux/files/usr/bin/bash
exec python3 "$TARGET" "\$@"
EOS
  chmod +x "$WRAP"
  log "Wrapper at $WRAP (run: devtools --help)"

  # --- rish ---
  echo ""
  info "Checking Shizuku/rish…"
  if have rish; then
    if rish -c 'id' 2>&1 | grep -q "uid="; then log "rish OK: $(rish -c 'id' 2>&1 | head -1)"; else warn "rish exists but not working — ensure Shizuku is running (green)"; fi
  else
    if [[ -f /data/local/tmp/shizuku/rish && -f /data/local/tmp/shizuku/rish_shizuku.dex ]]; then
      info "Found /data/local/tmp/shizuku/rish* — installing to \$PREFIX/bin/"
      cp /data/local/tmp/shizuku/rish* "$PREFIX/bin/" 2>/dev/null || true
      chmod +x "$PREFIX/bin/rish" 2>/dev/null || true
      if have rish && rish -c 'id' 2>&1 | grep -q "uid="; then log "rish installed: $(rish -c 'id' 2>&1 | head -1)"; else warn "rish copy failed (try manually: cp /data/local/tmp/shizuku/rish* \$PREFIX/bin/ && chmod +x \$PREFIX/bin/rish)"; fi
    else
      warn "rish not found. For Shizuku (non-root, no PC):"
      echo "    1) Start Shizuku via Wireless Debugging (Android 11+)"
      echo "    2) Shizuku → Use in terminal apps → Export files"
      echo "    3) cp /data/local/tmp/shizuku/rish* \$PREFIX/bin/ && chmod +x \$PREFIX/bin/rish"
      echo "    4) rish -c 'id'   # must show uid=2000(shell)"
    fi
    if [[ ! -f "$PREFIX/bin/shizuku" && -f /data/local/tmp/shizuku/rish ]]; then
      info "Tip: auto-activate Shizuku on boot needs a Termux script — see https://github.com/RikkaApps/Shizuku/discussions/462"
    fi
  fi

  # Phantom killer hint (Android 12+)
  if have rish; then
    if rish -c 'device_config get activity_manager max_phantom_processes' 2>&1 | grep -q "null\|2147483647"; then :; else warn "If you see 'Process completed (signal 9)', run: rish -c 'device_config put activity_manager max_phantom_processes 2147483647'"; fi
  fi
fi

echo ""
log "Done."
echo "  devtools --help          # or ./devtools.py --help"
echo "  devtools --shizuku       # non-root + Shizuku (recommended)"
echo "  devtools                 # rooted (auto)"
echo "  devtools 1234            # manual port (PC: adb tcpip 1234)"
echo "  devtools --list          # list tabs and exit"
echo "  Web UI: http://127.0.0.1:9223/   (also: /health, /api/tabs)"
echo ""
have adb || warn "adb still missing — pkg install android-tools"
