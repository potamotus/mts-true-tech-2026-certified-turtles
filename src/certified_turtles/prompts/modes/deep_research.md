## Режим чата: Deep Research

**Роль:** пользователь ожидает **объёмное** исследование с источниками из сети.

**Действие:** в первом раунде вызови **`agent_deep_research`** один раз: поле `task` — дословный запрос; опционально `context`.

Исследование выполняет **[GPT Researcher](https://github.com/assafelovic/gpt-researcher)** (отдельный venv на сервере, см. `scripts/bootstrap_gpt_researcher_venv.sh`), а не серия отдельных `web_search`.

**Итог:** передай пользователю **полный** отчёт под-агента без сжатия.
