from __future__ import annotations

import json
import mimetypes
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import requests

from certified_turtles.agent_debug_log import agent_logger, debug_clip

DEFAULT_BASE_URL = "https://api.gpt.mws.ru"

_mws_log = agent_logger("mws")


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
        try:
            timeout_sec = int(os.environ.get("MWS_HTTP_TIMEOUT_SEC", "120"))
        except (TypeError, ValueError):
            timeout_sec = 120
        timeout_sec = max(30, min(600, timeout_sec))
        try:
            retries = int(os.environ.get("MWS_HTTP_RETRIES", "2"))
        except (TypeError, ValueError):
            retries = 2
        retries = max(0, min(5, retries))

        raw: str | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    raw = resp.read().decode("utf-8")
                break
            except ConnectionError as e:
                if attempt >= retries:
                    raise MWSGPTError(
                        f"MWS разорвал соединение ({method} {path}) после {retries + 1} попыток: {e!s}. "
                        "Проверьте сеть, VPN, лимиты API и размер запроса. "
                        "Для агента: компактный каталог тулов включён по умолчанию (CT_AGENT_COMPACT_TOOL_CATALOG); "
                        "или используйте /v1/plain для чата без тулов.",
                        status=502,
                        body=str(e),
                    ) from e
                time.sleep(0.35 * (attempt + 1))
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
                err_body = e.read().decode("utf-8", errors="replace")
                raise MWSGPTError(e.reason or str(e), status=e.code, body=err_body) from e
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
        _mws_log.debug(
            "POST /v1/chat/completions model=%s messages=%s extra_keys=%s",
            model,
            len(messages),
            sorted(extra.keys()),
        )
        out = self._request("POST", "/v1/chat/completions", payload=body)
        _mws_log.debug("POST /v1/chat/completions response preview=\n%s", debug_clip(json.dumps(out, ensure_ascii=False)))
        return out

    def chat_completions_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **extra: Any,
    ) -> Iterator[dict[str, Any]]:
        """POST /v1/chat/completions с stream: true, разбор SSE `data: {...}` / `[DONE]`."""
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        body.update(extra)
        url = f"{self._base}/v1/chat/completions"
        headers = self._headers(json_body=True)
        try:
            timeout_sec = int(os.environ.get("MWS_HTTP_TIMEOUT_SEC", "120"))
        except (TypeError, ValueError):
            timeout_sec = 120
        timeout_sec = max(30, min(600, timeout_sec))
        _mws_log.debug(
            "POST /v1/chat/completions (stream) model=%s messages=%s extra_keys=%s",
            model,
            len(messages),
            sorted(extra.keys()),
        )
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout_sec, stream=True)
        except requests.RequestException as e:
            raise MWSGPTError(
                f"Сеть при POST /v1/chat/completions (stream): {e!s}",
                status=502,
                body=str(e),
            ) from e
        try:
            if r.status_code >= 400:
                err_body = r.text[:8000] if r.text else ""
                raise MWSGPTError(
                    f"Ошибка MWS chat/completions (stream): HTTP {r.status_code}",
                    status=r.status_code,
                    body=err_body,
                )
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
        finally:
            r.close()

    def completions(self, model: str, prompt: str, **extra: Any) -> Any:
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        body.update(extra)
        return self._request("POST", "/v1/completions", payload=body)

    def chat_completions_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **extra: Any,
    ) -> Iterator[bytes]:
        """POST /v1/chat/completions with stream=true. Yields raw SSE lines."""
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        body.update(extra)
        # Remove stream from extra if caller passed it explicitly.
        body["stream"] = True
        url = f"{self._base}/v1/chat/completions"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = self._headers(json_body=True)
        try:
            timeout_sec = max(30, min(600, int(os.environ.get("MWS_HTTP_TIMEOUT_SEC", "120"))))
        except (TypeError, ValueError):
            timeout_sec = 120
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout_sec)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise MWSGPTError(e.reason or str(e), status=e.code, body=err_body) from e
        except (ConnectionError, TimeoutError, socket.timeout, urllib.error.URLError) as e:
            raise MWSGPTError(f"Streaming connection failed: {e!s}", status=502, body=str(e)) from e
        try:
            for raw_line in resp:
                yield raw_line
        finally:
            resp.close()

    def embeddings(self, model: str, input_text: str, **extra: Any) -> Any:
        body: dict[str, Any] = {"model": model, "input": input_text}
        body.update(extra)
        return self._request("POST", "/v1/embeddings", payload=body)

    def images_generations(self, payload: dict[str, Any]) -> Any:
        """POST /v1/images/generations (OpenAI-совместимо). У MWS модели `qwen-image` работают здесь, а не в chat/completions."""
        return self._request("POST", "/v1/images/generations", payload=payload)

    def audio_transcriptions(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        model: str | None = None,
        language: str | None = None,
        prompt: str | None = None,
        response_format: str | None = None,
        temperature: float | None = None,
    ) -> Any:
        """POST /v1/audio/transcriptions (multipart), OpenAI-совместимо."""
        url = f"{self._base}/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        guessed, _ = mimetypes.guess_type(filename)
        mime = guessed or "application/octet-stream"
        files = {"file": (filename or "audio.bin", file_bytes, mime)}
        data: dict[str, Any] = {}
        data["model"] = (model or os.environ.get("CT_ASR_MODEL") or "whisper-1").strip()
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        if response_format:
            data["response_format"] = response_format
        if temperature is not None:
            data["temperature"] = str(temperature)
        try:
            timeout_sec = int(os.environ.get("MWS_AUDIO_TIMEOUT_SEC", os.environ.get("MWS_HTTP_TIMEOUT_SEC", "120")))
        except (TypeError, ValueError):
            timeout_sec = 120
        timeout_sec = max(30, min(600, timeout_sec))
        _mws_log.debug(
            "POST /v1/audio/transcriptions model=%s bytes=%s filename=%s",
            data.get("model"),
            len(file_bytes),
            filename,
        )
        try:
            r = requests.post(url, headers=headers, files=files, data=data, timeout=timeout_sec)
        except requests.RequestException as e:
            raise MWSGPTError(
                f"Сеть при POST /v1/audio/transcriptions: {e!s}",
                status=502,
                body=str(e),
            ) from e
        raw_text = r.text
        if r.status_code >= 400:
            raise MWSGPTError(
                f"Ошибка MWS audio/transcriptions: HTTP {r.status_code}",
                status=r.status_code,
                body=raw_text[:8000],
            )
        ct = (r.headers.get("Content-Type") or "").lower()
        if "application/json" in ct:
            try:
                return json.loads(raw_text)
            except json.JSONDecodeError as e:
                raise MWSGPTError(f"Ответ audio не JSON: {e}", body=raw_text[:2000]) from e
        return {"text": raw_text}
