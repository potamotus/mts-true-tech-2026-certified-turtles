"""HTTP-клиент для MWS Tables (APITable) — tabs.mts.ru/fusion/v1."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://tables.mws.ru/fusion/v1"


class MWSTablesError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def parse_datasheet_id(raw: str) -> str:
    """Извлекает datasheetId (dst*) из URL или принимает как есть."""
    raw = raw.strip().strip('"').strip("'")
    m = re.search(r"(dst[A-Za-z0-9]+)", raw)
    return m.group(1) if m else raw


class MWSTablesClient:
    """Тонкий HTTP-клиент: auth, pagination, error mapping."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
    ):
        self._token = token or os.environ.get("MWS_TABLES_API_TOKEN", "")
        if not self._token:
            raise ValueError("MWS_TABLES_API_TOKEN не задан")
        self._base = (base_url or os.environ.get("MWS_TABLES_API_BASE", DEFAULT_BASE_URL)).rstrip("/")

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        h: dict[str, str] = {"Authorization": f"Bearer {self._token}"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self._base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        timeout = 30
        req = urllib.request.Request(
            url, data=data, headers=self._headers(json_body=payload is not None), method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise MWSTablesError(e.reason or str(e), status=e.code, body=err_body) from e
        except (ConnectionError, TimeoutError, socket.timeout, urllib.error.URLError) as e:
            raise MWSTablesError(f"MWS Tables network error: {e!s}", status=502, body=str(e)) from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise MWSTablesError(f"Response not JSON: {e}", body=raw) from e

    # ── Records ──────────────────────────────────────────────

    def get_records(
        self,
        datasheet_id: str,
        *,
        view_id: str | None = None,
        filter_formula: str | None = None,
        sort: list[dict[str, str]] | None = None,
        fields: list[str] | None = None,
        page_size: int = 100,
        page_num: int = 1,
        max_records: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "pageSize": str(min(page_size, 1000)),
            "pageNum": str(page_num),
        }
        if view_id:
            params["viewId"] = view_id
        if filter_formula:
            params["filterByFormula"] = filter_formula
        if sort:
            params["sort"] = json.dumps(sort, ensure_ascii=False)
        if fields:
            for f in fields:
                params.setdefault("fields[]", f)
            # urllib.parse.urlencode не поддерживает repeated keys — формируем вручную
            base_params = {k: v for k, v in params.items() if k != "fields[]"}
            extra = "&".join(f"fields[]={urllib.parse.quote(f)}" for f in fields)
            url = f"{self._base}/datasheets/{datasheet_id}/records"
            qs = urllib.parse.urlencode(base_params)
            if qs:
                qs += "&" + extra
            else:
                qs = extra
            req = urllib.request.Request(
                f"{url}?{qs}", headers=self._headers(), method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                raise MWSTablesError(e.reason or str(e), status=e.code, body=err_body) from e
            except (ConnectionError, TimeoutError, socket.timeout, urllib.error.URLError) as e:
                raise MWSTablesError(f"MWS Tables network error: {e!s}", status=502) from e
            except json.JSONDecodeError as e:
                raise MWSTablesError(f"Response not JSON: {e}", body=raw) from e
        if max_records:
            params["maxRecords"] = str(max_records)
        return self._request("GET", f"/datasheets/{datasheet_id}/records", params=params)

    def create_records(self, datasheet_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/datasheets/{datasheet_id}/records",
            payload={"records": [{"fields": r} for r in records]},
        )

    def update_records(self, datasheet_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        """records: [{"recordId": "recXXX", "fields": {...}}, ...]"""
        return self._request(
            "PATCH",
            f"/datasheets/{datasheet_id}/records",
            payload={"records": records},
        )

    def delete_records(self, datasheet_id: str, record_ids: list[str]) -> dict[str, Any]:
        params_str = "&".join(f"recordIds={rid}" for rid in record_ids)
        url = f"{self._base}/datasheets/{datasheet_id}/records?{params_str}"
        req = urllib.request.Request(url, headers=self._headers(), method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise MWSTablesError(e.reason or str(e), status=e.code, body=err_body) from e
        except (ConnectionError, TimeoutError, socket.timeout, urllib.error.URLError) as e:
            raise MWSTablesError(f"MWS Tables network error: {e!s}", status=502) from e
        except json.JSONDecodeError as e:
            raise MWSTablesError(f"Response not JSON: {e}", body=raw) from e

    # ── Schema ───────────────────────────────────────────────

    def get_fields(self, datasheet_id: str) -> dict[str, Any]:
        return self._request("GET", f"/datasheets/{datasheet_id}/fields")

    def create_field(self, datasheet_id: str, name: str, field_type: str, *, property: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "type": field_type}
        if property:
            payload["property"] = property
        return self._request("POST", f"/datasheets/{datasheet_id}/fields", payload=payload)

    def delete_field(self, datasheet_id: str, field_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/datasheets/{datasheet_id}/fields/{field_id}")

    def get_views(self, datasheet_id: str) -> dict[str, Any]:
        return self._request("GET", f"/datasheets/{datasheet_id}/views")

    # ── Attachments ───────────────────────────────────────────

    def upload_attachment(self, datasheet_id: str, file_path: str) -> dict[str, Any]:
        """Upload a file as attachment. Returns attachment token for use in record fields."""
        import mimetypes
        boundary = "----CertifiedTurtlesBoundary"
        filename = os.path.basename(file_path)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(file_path, "rb") as f:
            file_data = f.read()
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
        url = f"{self._base}/datasheets/{datasheet_id}/attachments"
        headers = self._headers()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise MWSTablesError(e.reason or str(e), status=e.code, body=err_body) from e
        except (ConnectionError, TimeoutError, socket.timeout, urllib.error.URLError) as e:
            raise MWSTablesError(f"MWS Tables network error: {e!s}", status=502) from e

    # ── Nodes ────────────────────────────────────────────────

    def list_nodes(self, space_id: str) -> dict[str, Any]:
        return self._request("GET", f"/spaces/{space_id}/nodes")

    def list_spaces(self) -> dict[str, Any]:
        return self._request("GET", "/spaces")

    def create_datasheet(
        self,
        space_id: str,
        name: str,
        *,
        folder_id: str | None = None,
        description: str | None = None,
        fields: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        if folder_id:
            payload["folderId"] = folder_id
        if description:
            payload["description"] = description
        if fields:
            payload["fields"] = fields
        return self._request("POST", f"/spaces/{space_id}/datasheets", payload=payload)


_client: MWSTablesClient | None = None


def get_client() -> MWSTablesClient:
    """Lazy singleton; raises ValueError if token not configured."""
    global _client
    if _client is None:
        _client = MWSTablesClient()
    return _client


def is_configured() -> bool:
    return bool(os.environ.get("MWS_TABLES_API_TOKEN"))
