#!/usr/bin/env bash
# Run Himmy's OWN API (the workspace backend): one small FastAPI app exposing
# /ask, /index, /health over localhost. The workspace UI talks to this. Zotero must be
# running in the background (it's the library engine).
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
set -a; [ -f .env ] && . ./.env; set +a

source .venv/bin/activate

PORT="${HIMMY_APP_PORT:-8131}"
echo "Himmy workspace API → http://localhost:${PORT}"
exec python -m himmy_app.server
