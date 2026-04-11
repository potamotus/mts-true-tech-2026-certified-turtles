from __future__ import annotations

from fastapi import FastAPI

from certified_turtles.api.agent import router as agent_router
from certified_turtles.api.openai_proxy import router as openai_proxy_router

app = FastAPI(
    title="Certified Turtles / GPTHub API",
    version="0.3.0",
    description=(
        "Единый фасад над MWS GPT: OpenAI-совместимый прокси для Open WebUI "
        "(`/v1/*`) и агент с tool calling (`/api/v1/agent/chat`)."
    ),
)

app.include_router(openai_proxy_router)
app.include_router(agent_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
