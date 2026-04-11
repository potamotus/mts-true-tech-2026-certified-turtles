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

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "certified_turtles.main:app", "--host", "0.0.0.0", "--port", "8000"]
