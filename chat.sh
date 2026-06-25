#!/usr/bin/env bash
# Talk to Himmy in the terminal via himmy's own chat REPL (one persistent thread).
# Equivalent to running `himmy-app`, but uses himmy's chat surface directly.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
set -a; [ -f .env ] && . ./.env; set +a

source .venv/bin/activate

PROVIDER="${HIMMY_APP_PROVIDER:-openrouter}"
MODEL="${HIMMY_APP_MODEL:-google/gemini-2.5-flash}"

exec himmy chat agent/agent.yaml --provider "${PROVIDER}" --model "${MODEL}"
