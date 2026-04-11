from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; CertifiedTurtlesBot/0.1; +https://mts-true-hack-certified-turtles.vercel.app)"
_SKIP_TAGS = frozenset({"script", "style", "noscript", "head", "svg", "nav", "footer", "aside"})


class _TextExtractor(HTMLParser):
    """HTML → plain text: собирает data-узлы, пропуская шумные контейнеры."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0
        self._title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip > 0:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and self._title is None:
            t = data.strip()
            if t:
                self._title = t
        if self._skip == 0:
            self._parts.append(data)

    @property
    def title(self) -> str:
        return self._title or ""

    @property
    def text(self) -> str:
        joined = " ".join(self._parts)
        return re.sub(r"\s+", " ", joined).strip()


def fetch_url_text(url: str, *, max_chars: int = 8000, timeout: int = 15) -> dict[str, str]:
    """Скачивает страницу и возвращает {url, title, text}. Режет до `max_chars`.

    Намеренно просто: stdlib, без headless-браузера. Для SPA/JS-сайтов может вернуть пустой текст.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} при загрузке {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Сетевой сбой при загрузке {url}: {e.reason}") from e

    parser = _TextExtractor()
    try:
        parser.feed(raw)
    except Exception:  # noqa: BLE001 - HTMLParser падает редко, но на битых документах возможно
        logger.warning("html parse failed for %s; fallback to raw strip", url)
        fallback = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", fallback).strip()
        return {"url": final_url, "title": "", "text": text[:max_chars]}

    text = parser.text
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return {"url": final_url, "title": parser.title, "text": text}
