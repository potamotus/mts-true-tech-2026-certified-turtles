from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .storage import parse_frontmatter

MAX_STATIC_INSTRUCTION_BYTES = 40_000
MAX_INCLUDE_DEPTH = 5
MAX_INCLUDE_FILE_CHARS = 40_000

# Matches @path (including escaped spaces) on its own line.
_INCLUDE_RE = re.compile(r"(?:^|\n)\s*@((?:[^\s\\]|\\ )+)")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConditionalRule:
    path: Path
    content: str
    globs: tuple[str, ...]


def _is_inside_code_block(text: str, pos: int) -> bool:
    count = 0
    for m in _FENCE_RE.finditer(text):
        if m.start() >= pos:
            break
        count += 1
    return count % 2 == 1


def _parse_paths_field(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if isinstance(x, str)]
        except json.JSONDecodeError:
            pass
    return [x.strip().strip('"').strip("'") for x in raw.split(",") if x.strip()]


def _instruction_roots(cwd: Path) -> tuple[list[tuple[str, Path]], list[tuple[Path, list[str]]]]:
    """Returns (unconditional_roots, conditional_rule_candidates)."""
    roots: list[tuple[str, Path]] = []
    conditional: list[tuple[Path, list[str]]] = []
    managed = os.environ.get("CT_MANAGED_CLAUDE_MD")
    if managed:
        roots.append(("Managed", Path(managed).expanduser()))
    roots.append(("User", Path.home() / ".claude" / "CLAUDE.md"))
    seen: set[Path] = set()
    current = cwd.resolve(strict=False)
    parents = [current, *current.parents]
    for base in reversed(parents):
        if base in seen:
            continue
        seen.add(base)
        roots.append(("Project", base / "CLAUDE.md"))
        roots.append(("Project", base / ".claude" / "CLAUDE.md"))
        rules_dir = base / ".claude" / "rules"
        if rules_dir.is_dir():
            for rule in sorted(rules_dir.glob("*.md")):
                fm = parse_frontmatter(rule.read_text(encoding="utf-8", errors="replace")[:1024])
                paths_raw = fm.get("paths")
                if paths_raw:
                    conditional.append((rule, _parse_paths_field(paths_raw)))
                else:
                    roots.append(("Project", rule))
        roots.append(("Local", base / "CLAUDE.local.md"))
    return roots, conditional


def _resolve_include(base_path: Path, raw_target: str) -> Path:
    target = raw_target.replace("\\ ", " ").strip()
    if "#" in target:
        target = target.split("#", 1)[0]
    target = target.strip()
    if target.startswith("~/"):
        return Path(target).expanduser()
    candidate = Path(target)
    if candidate.is_absolute():
        return candidate
    return (base_path.parent / candidate).resolve(strict=False)


def _read_with_includes(path: Path, *, seen: set[Path], depth: int = 0) -> str:
    if depth > MAX_INCLUDE_DEPTH:
        _log.warning("@include depth limit (%d) reached at %s", MAX_INCLUDE_DEPTH, path)
        return ""
    resolved = path.resolve(strict=False)
    if resolved in seen:
        return ""
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    if not is_file:
        if depth > 0:
            _log.debug("@include target not found: %s", path)
        return ""
    seen.add(resolved)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _log.warning("@include failed to read: %s", path)
        return ""
    if len(raw) > MAX_INCLUDE_FILE_CHARS:
        _log.warning("@include file truncated (%d > %d chars): %s", len(raw), MAX_INCLUDE_FILE_CHARS, path)
        raw = raw[:MAX_INCLUDE_FILE_CHARS]
    out_parts: list[str] = []
    last = 0
    for match in _INCLUDE_RE.finditer(raw):
        if _is_inside_code_block(raw, match.start()):
            continue
        out_parts.append(raw[last : match.start()])
        include_path = _resolve_include(path, match.group(1))
        out_parts.append(_read_with_includes(include_path, seen=seen, depth=depth + 1))
        last = match.end()
    out_parts.append(raw[last:])
    return "".join(out_parts).strip()


def load_static_instruction_prompt(cwd: str | None = None) -> str:
    base = Path(cwd or os.getcwd()).resolve(strict=False)
    unconditional, _ = _instruction_roots(base)
    sections: list[str] = []
    seen: set[Path] = set()
    for kind, path in unconditional:
        if not path.is_file():
            continue
        content = _read_with_includes(path, seen=seen)
        if not content:
            continue
        sections.append(f"## {kind}: {path}\n{content}")
    if not sections:
        return ""
    prompt = (
        "Codebase and user instructions are shown below. These instructions override default behavior and must be followed exactly when they apply.\n\n"
        + "\n\n".join(sections)
    )
    encoded = prompt.encode("utf-8", errors="replace")
    if len(encoded) > MAX_STATIC_INSTRUCTION_BYTES:
        prompt = encoded[:MAX_STATIC_INSTRUCTION_BYTES].decode("utf-8", errors="ignore").rstrip()
    return prompt


def load_conditional_rules(cwd: str | None = None) -> list[ConditionalRule]:
    base = Path(cwd or os.getcwd()).resolve(strict=False)
    _, candidates = _instruction_roots(base)
    rules: list[ConditionalRule] = []
    seen: set[Path] = set()
    for path, globs in candidates:
        content = _read_with_includes(path, seen=seen)
        if content:
            rules.append(ConditionalRule(path=path, content=content, globs=tuple(globs)))
    return rules
