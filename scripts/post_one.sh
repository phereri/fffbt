#!/usr/bin/env bash
# post_one.sh — standalone Trial Reel poster (Linux/macOS).
#
# Loads runner.env into the environment, sets PYTHONPATH=src, then runs
#   python -m runner post-one <your args...>
#
# Usage (from the repo root):
#   ./scripts/post_one.sh --device 100.100.57.41:5555 --video /clips/a.mp4 --caption "hi"
#   ./scripts/post_one.sh --device 100.100.57.41:5555 --video "https://bucket.s3...mp4?sig=.." --caption "hi" --hashtags trial,reels
#
# Prereqs: venv created and deps installed; runner.env filled in; phone prepared
# (IG logged in, Mobilerun Portal bound). This NEVER touches the database.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/runner.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "runner.env not found at $ENV_FILE. Copy config/runner.env.example to runner.env and fill it in." >&2
    exit 2
fi

# Load runner.env: export every non-comment KEY=VALUE line.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

export PYTHONPATH="src"

# Prefer the repo venv python; fall back to python3 on PATH.
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || command -v python)"

exec "$PY" -m runner post-one "$@"
