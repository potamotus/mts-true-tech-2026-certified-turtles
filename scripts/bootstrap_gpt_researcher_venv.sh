#!/usr/bin/env bash
# Создаёт .venv-gpt-researcher и ставит gpt-researcher из requirements (отдельный граф зависимостей).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${GPT_RESEARCHER_VENV:-$ROOT/.venv-gpt-researcher}"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -U pip
"$VENV/bin/pip" install -r "$ROOT/externals/requirements-gpt-researcher.txt"
echo "GPT Researcher venv: $VENV/bin/python"
