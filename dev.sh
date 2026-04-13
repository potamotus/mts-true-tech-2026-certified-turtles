#!/usr/bin/env bash
# Fast dev: open-webui in Docker (built once), API locally with hot-reload.
# First run builds open-webui (~10 min). After that — instant startup.
set -euo pipefail

# Start open-webui (--no-deps skips building/starting the api container)
echo "→ Starting open-webui in Docker..."
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --no-deps open-webui
echo "→ open-webui: http://localhost:${OPEN_WEBUI_PORT:-3000}"

# Load .env if present
[ -f .env ] && set -a && source .env && set +a

# Run API locally with hot-reload
echo "→ Starting API on :8000 (Ctrl+C to stop)..."
exec uv run uvicorn certified_turtles.main:app \
  --reload --host 0.0.0.0 --port 8000
