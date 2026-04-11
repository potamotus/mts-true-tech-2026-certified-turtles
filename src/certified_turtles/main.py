from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from certified_turtles.tools.builtins.google_docs import google_docs_capability_dict

from certified_turtles.api.agent import router as agent_router
from certified_turtles.api.files import router as files_router
from certified_turtles.api.openai_proxy import router as openai_proxy_router
from certified_turtles.api.uploads import router as uploads_router

app = FastAPI(
    title="Certified Turtles / GPTHub API",
    version="0.3.0",
    description=(
        "Единый фасад над MWS GPT: OpenAI-совместимый прокси для Open WebUI "
        "(`/v1/*` — агент с тулами; `/v1/plain/*` — обычный чат), "
        "`/api/v1/agent/chat`, загрузки `POST /api/v1/uploads`, раздача `/files/*`."
    ),
)

app.include_router(openai_proxy_router)
app.include_router(files_router)
app.include_router(agent_router, prefix="/api/v1")
app.include_router(uploads_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, Any]:
    """Статус API и возможности (в т.ч. Google Docs), чтобы админ/пользователь видели, что включено."""
    return {
        "status": "ok",
        "capabilities": {
            "google_docs": google_docs_capability_dict(),
        },
    }
