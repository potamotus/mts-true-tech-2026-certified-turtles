# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# слой зависимостей (кеш при неизменном lock)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-group dev --no-install-project --extra google

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-group dev --extra google

# GPT Researcher (assafelovic/gpt-researcher) — отдельный venv, не в uv.lock основного приложения
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/gpt-researcher-venv \
    && /opt/gpt-researcher-venv/bin/pip install --no-cache-dir -U pip setuptools wheel \
    && /opt/gpt-researcher-venv/bin/pip install --no-cache-dir -r externals/requirements-gpt-researcher.txt

ENV GPT_RESEARCHER_PYTHON=/opt/gpt-researcher-venv/bin/python

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "certified_turtles.main:app", "--host", "0.0.0.0", "--port", "8000"]
