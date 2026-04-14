## Под-агент deep_research (заменён на GPT Researcher)

Исследование выполняет **[GPT Researcher](https://github.com/assafelovic/gpt-researcher)** (PyPI `gpt-researcher`) в **отдельном виртуальном окружении**, а не самописный цикл `web_search` / `fetch_url`.

**Локальная установка венва:** из корня репозитория выполните `bash scripts/bootstrap_gpt_researcher_venv.sh` (создаётся `.venv-gpt-researcher`). Либо укажите путь к интерпретатору: переменная окружения `GPT_RESEARCHER_PYTHON`.

**API для LLM:** ключ и base URL берутся из `OPENAI_*` или из `MWS_API_KEY` / `MWS_API_BASE`, как в остальном проекте. Поиск по умолчанию: `RETRIEVER=duckduckgo`, если нет `TAVILY_API_KEY`.

Этот текст остаётся в system для совместимости формата; фактическая работа — в рантайме через subprocess и воркер `scripts/gpt_researcher_worker.py`.
