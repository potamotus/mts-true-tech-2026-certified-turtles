from __future__ import annotations

import json
import os
import socket
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


def http_status_for_mws_error(e: MWSGPTError) -> int:
    """HTTP-код для прокси: ошибки MWS как есть, сетевые/таймауты — 502/504."""
    if e.status is not None and 400 <= int(e.status) < 600:
        return int(e.status)
    return 502


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
            timeout_sec = int(os.environ.get("MWS_HTTP_TIMEOUT_SEC", "120"))
        except (TypeError, ValueError):
            timeout_sec = 120
        timeout_sec = max(30, min(600, timeout_sec))
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except TimeoutError as e:
            raise MWSGPTError(
                f"Таймаут MWS при {method} {path} (лимит {timeout_sec}s чтения ответа). "
                "Попробуйте другую модель, уменьшите контекст или max_tool_rounds.",
                status=504,
                body=str(e),
            ) from e
        except socket.timeout as e:
            raise MWSGPTError(
                f"Таймаут сокета MWS при {method} {path} ({timeout_sec}s).",
                status=504,
                body=str(e),
            ) from e
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise MWSGPTError(e.reason or str(e), status=e.code, body=raw) from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            msg = repr(reason) if reason is not None else str(e)
            raise MWSGPTError(
                f"Сеть при обращении к MWS ({method} {path}): {msg}",
                status=502,
                body=str(e),
            ) from e

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
        messages: list[dict[str, Any]],
        **extra: Any,
    ) -> Any:
        """POST /v1/chat/completions. В `extra` можно передать tools, tool_choice, response_format и др."""
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
