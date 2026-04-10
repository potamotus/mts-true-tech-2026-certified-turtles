from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "https://api.gpt.mws.ru"


class MWSGPTError(Exception):
    """Ошибка HTTP или не-JSON ответа от API."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class MWSGPTClient:
    """HTTP-клиент для GET /v1/models и POST chat/completions, completions, embeddings."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
    ):
        key = api_key or os.environ.get("MWS_API_KEY") or os.environ.get("MWS_GPT_API_KEY")
        if not key:
            raise ValueError(
                "Укажите api_key или переменные окружения MWS_API_KEY / MWS_GPT_API_KEY"
            )
        self._api_key = key
        self._base = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._api_key}"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base}{path}"
        data: bytes | None = None
        headers = self._headers(json_body=payload is not None)
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise MWSGPTError(e.reason or str(e), status=e.code, body=raw) from e

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise MWSGPTError(f"Ответ не JSON: {e}", body=raw) from e

    def list_models(self) -> Any:
        return self._request("GET", "/v1/models")

    def chat_completions(
        self,
        model: str,
        messages: list[dict[str, str]],
        **extra: Any,
    ) -> Any:
        body: dict[str, Any] = {"model": model, "messages": messages}
        body.update(extra)
        return self._request("POST", "/v1/chat/completions", payload=body)

    def completions(self, model: str, prompt: str, **extra: Any) -> Any:
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        body.update(extra)
        return self._request("POST", "/v1/completions", payload=body)

    def embeddings(self, model: str, input_text: str, **extra: Any) -> Any:
        body: dict[str, Any] = {"model": model, "input": input_text}
        body.update(extra)
        return self._request("POST", "/v1/embeddings", payload=body)
