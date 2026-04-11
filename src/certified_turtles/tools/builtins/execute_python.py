from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import uuid
from typing import Any

from certified_turtles.tools.presentation import _storage_dir  # noqa: PLC2701
from certified_turtles.tools.registry import ToolSpec, register_tool

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
            self.error = "Используй pd.read_csv / pathlib или только CT_RUN_OUTPUT_DIR для вывода; прямой open() запрещён."
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


def _handle_execute_python(arguments: dict[str, Any]) -> str:
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        return json.dumps({"error": "Нужен непустой параметр code (Python)."}, ensure_ascii=False)

    err = _validate_code(code)
    if err:
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
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(preamble + "\n" + code)

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
        return json.dumps(
            {"error": "timeout", "detail": f"Лимит {_TIMEOUT_SEC}s"},
            ensure_ascii=False,
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
        description=(
            "Выполнить ограниченный Python для анализа данных и построения графиков (matplotlib/numpy/pandas). "
            "Код запускается в отдельном процессе с таймаутом; запрещены опасные import и вызовы open/eval/exec. "
            "Для сохранения графиков используй plt.savefig(os.path.join(CT_RUN_OUTPUT_DIR, 'plot.png'))."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Полный Python-скрипт (без интерактива).",
                },
            },
            "required": ["code"],
        },
        handler=_handle_execute_python,
    )
)
