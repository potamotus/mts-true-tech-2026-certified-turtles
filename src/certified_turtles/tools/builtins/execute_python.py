from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import uuid
from typing import Any

from certified_turtles.agent_debug_log import agent_logger, debug_clip
from certified_turtles.tools.builtins.workspace_file_path import (
    _looks_like_placeholder_file_id,
    resolve_workspace_upload_file,
)
from certified_turtles.tools.presentation import _storage_dir  # noqa: PLC2701
from certified_turtles.prompts import load_prompt
from certified_turtles.tools.registry import ToolSpec, register_tool
from certified_turtles.tools.workspace_storage import uploads_dir

_py_log = agent_logger("execute_python")

_TIMEOUT_SEC = int(os.environ.get("PYTHON_TOOL_TIMEOUT_SEC", "45"))

_ALLOWED_MODULE_PREFIXES: tuple[str, ...] = (
    "math",
    "json",
    "re",
    "datetime",
    "random",
    "statistics",
    "itertools",
    "functools",
    "collections",
    "csv",
    "io",
    "base64",
    "typing",
    "decimal",
    "string",
    "textwrap",
    "uuid",
    "pprint",
    "copy",
    "enum",
    "fractions",
    "hashlib",
    "numpy",
    "matplotlib",
    "pandas",
    "openpyxl",
    "pathlib",
    "sys",
    # HTTP из песочницы: URL/ключ из запроса пользователя (не хардкод провайдера).
    "urllib",
    "http.client",
    "ssl",
    "requests",
)

_FORBIDDEN_CALL_NAMES = frozenset({"eval", "exec", "compile", "__import__", "input"})


def _module_allowed(name: str | None) -> bool:
    if not name:
        return False
    for p in _ALLOWED_MODULE_PREFIXES:
        if name == p or name.startswith(f"{p}."):
            return True
    return False


class _Guard(ast.NodeVisitor):
    def __init__(self) -> None:
        self.error: str | None = None

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            if not _module_allowed(alias.name):
                self.error = f"Запрещённый import: {alias.name}"
                return
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.level and node.level > 0:
            self.error = "Относительные import запрещены"
            return
        if not _module_allowed(node.module):
            self.error = f"Запрещённый import from: {node.module}"
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALL_NAMES:
            self.error = f"Запрещённый вызов: {func.id}()"
            return
        if isinstance(func, ast.Name) and func.id == "open":
            self.error = "Используй pd.read_csv / read_excel по пути строки; прямой open() запрещён."
            return
        if isinstance(func, ast.Attribute) and func.attr == "open":
            self.error = "Метод .open() запрещён; чтение таблиц только через pd.read_csv(path) / pd.read_excel(path, engine='openpyxl')."
            return
        self.generic_visit(node)


def _validate_code(code: str) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax_error: {e}"
    g = _Guard()
    g.visit(tree)
    return g.error


def _public_base_url() -> str:
    return os.environ.get("PUBLIC_API_BASE_URL", "http://localhost:8000").rstrip("/")


def _normalize_llm_python(code: str) -> str:
    """LLM иногда присылает весь скрипт одной строкой с буквальными \n вместо переводов строки."""
    out = code
    if "\n" not in out and "\\n" in out:
        out = out.replace("\\r\\n", "\n").replace("\\n", "\n")
    if "\t" not in out and "\\t" in out:
        out = out.replace("\\t", "\t")
    return out


def _infer_file_id_from_code(code: str) -> str | None:
    """Если модель не передала file_id аргументом, пробуем вытащить его из кода."""
    try:
        root = uploads_dir()
        names = sorted(p.name for p in root.iterdir() if p.is_file())
    except OSError:
        return None
    hits = [name for name in names if name in code]
    uniq = sorted(set(hits))
    if len(uniq) == 1:
        return uniq[0]
    return None


def _file_arg_constant(arg_name: str) -> str:
    if arg_name == "file_id":
        return "CT_DATA_FILE_ABSPATH"
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", arg_name.removeprefix("file_id_")).strip("_").upper() or "EXTRA"
    return f"CT_DATA_FILE_ABSPATH_{suffix}"


def _collect_bound_files(arguments: dict[str, Any], code: str) -> dict[str, tuple[str, str]]:
    bound: dict[str, tuple[str, str]] = {}
    for key, value in arguments.items():
        if key != "file_id" and not key.startswith("file_id_"):
            continue
        fid = str(value or "").strip()
        if not fid:
            continue
        if _looks_like_placeholder_file_id(fid):
            raise ValueError("file_id_placeholder")
        resolved = resolve_workspace_upload_file(fid)
        if resolved is None:
            raise FileNotFoundError(fid)
        bound[key] = (fid, str(resolved[1].resolve()))
    if bound:
        return bound
    inferred = _infer_file_id_from_code(code)
    if not inferred:
        return {}
    resolved = resolve_workspace_upload_file(inferred)
    if resolved is None:
        return {}
    return {"file_id": (inferred, str(resolved[1].resolve()))}


def _rewrite_code_with_bound_files(code: str, bound: dict[str, tuple[str, str]]) -> str:
    """Чинит самые частые артефакты LLM вокруг file_id, не ломая остальной код."""
    out = code
    for arg_name, (fid, _) in bound.items():
        const_name = _file_arg_constant(arg_name)
        out = re.sub(
            rf"workspace_file_path\(\s*(['\"]){re.escape(fid)}\1\s*\)\s*\[\s*(['\"])absolute_path\2\s*\]",
            const_name,
            out,
        )
        out = re.sub(
            rf"(pd\.read_(?:csv|excel)\(\s*)(['\"]){re.escape(fid)}\2",
            rf"\1_ct_path('{fid}')",
            out,
        )
        out = re.sub(
            rf"(?m)^(\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*)(['\"]){re.escape(fid)}\2(\s*(?:#.*)?)$",
            rf"\1_ct_path('{fid}')\3",
            out,
        )
    out = re.sub(r"\bCT_DATA_FILE_ABSPATH\s*\+\s*([A-Za-z_][A-Za-z0-9_]*)", r"_ct_path(\1)", out)
    out = re.sub(r"\bCT_DATA_FILE_ABSPATH\s*\+\s*(['\"][^'\"]+['\"])", r"_ct_path(\1)", out)
    out = out.replace("workspace_file_path(CT_DATA_FILE_ABSPATH)", "workspace_file_path(_ct_path(CT_DATA_FILE_ABSPATH))")
    return out


def _handle_execute_python(arguments: dict[str, Any]) -> str:
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        return json.dumps({"error": "Нужен непустой параметр code (Python)."}, ensure_ascii=False)

    code = _normalize_llm_python(code)
    data_preamble = ""
    try:
        bound_files = _collect_bound_files(arguments, code)
    except ValueError:
        return json.dumps(
            {
                "error": "file_id_placeholder",
                "detail": (
                    "В file_id тула execute_python нужен реальный идентификатор из file_id=\"…\" в [CT: …], "
                    "а не шаблон в квадратных скобках."
                ),
            },
            ensure_ascii=False,
        )
    except FileNotFoundError:
        return json.dumps(
            {"error": "file_not_found", "detail": "Нет файла для file_id; сначала workspace_file_path или загрузка."},
            ensure_ascii=False,
        )

    if bound_files:
        code = _rewrite_code_with_bound_files(code, bound_files)
        primary_path = bound_files.get("file_id", next(iter(bound_files.values())))[1]
        mapping = {fid: path for fid, path in bound_files.values()}
        constants = "\n".join(
            f"{_file_arg_constant(arg_name)} = {path!r}" for arg_name, (_, path) in bound_files.items()
        )
        data_preamble = (
            f"CT_DATA_FILE_ABSPATH = {primary_path!r}\n"
            f"{constants}\n"
            f"CT_FILE_ID_TO_PATH = {mapping!r}\n"
            "CT_FILE_PATH_TO_ID = {v: k for k, v in CT_FILE_ID_TO_PATH.items()}\n"
            "def _ct_path(value):\n"
            "    return CT_FILE_ID_TO_PATH.get(value, value) if isinstance(value, str) else value\n"
            "def workspace_file_path(file_id):\n"
            "    path = _ct_path(file_id)\n"
            "    if not isinstance(path, str):\n"
            "        raise FileNotFoundError(f'Unsupported file handle: {file_id!r}')\n"
            "    real_file_id = CT_FILE_PATH_TO_ID.get(path, file_id)\n"
            "    return {'file_id': real_file_id, 'absolute_path': path}\n"
        )

    err = _validate_code(code)
    if err:
        _py_log.debug("validation_failed detail=%s code_preview=\n%s", err, debug_clip(code))
        return json.dumps({"error": "validation_failed", "detail": err}, ensure_ascii=False)

    out_root = _storage_dir() / "python_runs"
    out_root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    run_dir = out_root / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except OSError:
        return json.dumps({"error": "run_dir_exists", "detail": "Повторите вызов."}, ensure_ascii=False)

    run_dir_str = str(run_dir.resolve())
    script_path = str(run_dir / "user_code.py")
    preamble = (
        "import os\n"
        f"CT_RUN_OUTPUT_DIR = {run_dir_str!r}\n"
        "os.makedirs(CT_RUN_OUTPUT_DIR, exist_ok=True)\n"
        "os.environ.setdefault('MPLBACKEND', 'Agg')\n"
        + data_preamble
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(preamble + "\n" + code)

    _py_log.debug("run start run_id=%s script=%s code=\n%s", run_id, script_path, debug_clip(code))

    env = os.environ.copy()
    env["MPLBACKEND"] = "Agg"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            cwd=run_dir_str,
            env=env,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        _py_log.debug("run timeout run_id=%s after %ss", run_id, _TIMEOUT_SEC)
        return json.dumps(
            {"error": "timeout", "detail": f"Лимит {_TIMEOUT_SEC}s"},
            ensure_ascii=False,
        )

    _py_log.debug(
        "run end run_id=%s returncode=%s stdout=\n%s\nstderr=\n%s",
        run_id,
        proc.returncode,
        debug_clip(proc.stdout),
        debug_clip(proc.stderr),
    )

    artifacts: list[dict[str, str]] = []
    for name in sorted(os.listdir(run_dir_str)):
        if name == "user_code.py":
            continue
        p = os.path.join(run_dir_str, name)
        if os.path.isfile(p):
            artifacts.append({"name": name, "url": f"{_public_base_url()}/files/python_runs/{run_id}/{name}"})

    return json.dumps(
        {
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-24000:],
            "stderr": (proc.stderr or "")[-24000:],
            "artifacts": artifacts,
            "run_id": run_id,
            "note": (
                "Графики сохраняй в CT_RUN_OUTPUT_DIR (абсолютный путь уже в окружении скрипта) "
                "или в текущую директорию процесса; файлы отдаются через /files/python_runs/{run_id}/…"
            ),
        },
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="execute_python",
        description=load_prompt("execute_python_tool_description.txt").strip(),
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Полный Python-скрипт (без интерактива).",
                },
                "file_id": {
                    "type": "string",
                    "description": (
                        "Необязательно: реальный file_id из [CT:…] или ответа workspace_file_path; "
                        "тогда в коде доступна строка CT_DATA_FILE_ABSPATH."
                    ),
                },
                "file_id_2": {
                    "type": "string",
                    "description": "Необязательно: второй загруженный файл; константа CT_DATA_FILE_ABSPATH_2.",
                },
            },
            "required": ["code"],
        },
        handler=_handle_execute_python,
    )
)
