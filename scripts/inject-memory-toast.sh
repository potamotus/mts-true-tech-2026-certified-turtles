#!/bin/sh
# Inject memory-toast script into OpenWebUI's index.html before startup.
# Finds all index.html files in the OpenWebUI static dirs and appends a <script> tag.

# PUBLIC_API_BASE_URL comes from .env / docker-compose; falls back to localhost.
API_URL="${PUBLIC_API_BASE_URL:-http://localhost:8000}"
SCRIPT_TAG="<script src=\"${API_URL}/static/memory-toast.js\" defer></script>"

for html in \
  /app/backend/open_webui/static/index.html \
  /app/build/index.html \
  /app/backend/static/index.html; do
  if [ -f "$html" ]; then
    if ! grep -q "memory-toast" "$html"; then
      sed -i "s|</head>|${SCRIPT_TAG}</head>|" "$html"
      echo "[inject] Patched $html"
    else
      echo "[inject] Already patched: $html"
    fi
  fi
done

# Hand off to the original entrypoint
exec "$@"
