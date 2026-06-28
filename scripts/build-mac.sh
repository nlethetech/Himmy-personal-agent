#!/usr/bin/env bash
#
# Build the distributable macOS Himmy.app + Himmy.dmg from a clean checkout.
#
# Produces a SELF-CONTAINED app: the Python backend is frozen with PyInstaller and bundled
# inside the .app, so an end user needs no Python, no venv, and no terminal — they just
# double-click the .dmg, drag Himmy to Applications, and (first launch only) right-click → Open.
#
# Usage:  ./scripts/build-mac.sh
# Output: desktop/release/Himmy-<version>-arm64.dmg
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV_PY="$ROOT/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "!! No project venv at .venv — create it first (uv venv && uv pip install -e '.[...]')." >&2
  exit 1
fi

echo "==> [1/4] Ensuring PyInstaller is available"
"$VENV_PY" -c "import PyInstaller" 2>/dev/null || "$VENV_PY" -m pip install "pyinstaller>=6.6"

echo "==> [2/4] Freezing the Python backend (himmy-backend)"
"$VENV_PY" -m PyInstaller packaging/himmy-backend.spec \
  --distpath packaging/dist --workpath packaging/build --noconfirm

# Quick smoke test of the frozen backend before we wrap it in an app.
echo "    smoke-testing the frozen backend…"
SMOKE_DIR="$(mktemp -d)"
HIMMY_APP_PORT=8159 HIMMY_APP_DATA_DIR="$SMOKE_DIR" HIMMY_SECRETS=env \
  packaging/dist/himmy-backend/himmy-backend >/tmp/himmy-build-smoke.log 2>&1 &
SMOKE_PID=$!
ok=""
for _ in $(seq 1 20); do
  if curl -fsS -m 2 http://127.0.0.1:8159/health >/dev/null 2>&1; then ok=1; break; fi
  kill -0 "$SMOKE_PID" 2>/dev/null || break
  sleep 1
done
kill "$SMOKE_PID" 2>/dev/null || true
rm -rf "$SMOKE_DIR"
if [[ -z "$ok" ]]; then
  echo "!! Frozen backend failed to start — see /tmp/himmy-build-smoke.log" >&2
  exit 1
fi
echo "    frozen backend OK ✓"

echo "==> [3/4] Building the web UI (vite)"
cd "$ROOT/desktop"
[[ -d node_modules ]] || npm install
npm run build

echo "==> [4/4] Packaging the macOS app + dmg (electron-builder, ad-hoc signed)"
./node_modules/.bin/electron-builder --mac

echo
echo "Done. Installer:"
ls -1 "$ROOT/desktop/release"/*.dmg 2>/dev/null || true
echo
echo "First launch on another Mac (unsigned app): right-click Himmy → Open → Open."
