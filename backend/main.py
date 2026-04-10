from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(
    title="Certified Turtles / GPTHub API",
    version="0.1.0",
    description="Базовая точка входа; дальше — прокси MWS GPT и оркестрация.",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
