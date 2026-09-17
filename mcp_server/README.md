# mcp_server — MCP-сервер керування сервісом

Host-процес, через який ШІ-асистент керує живим стеком: акаунти ТГ, моніторинги,
збори, черги конвеєрів, контейнери.

```bash
uv venv .venv --python 3.11
VIRTUAL_ENV=.venv uv pip install -r requirements.txt
```

Реєстрація для Claude Code — у `.mcp.json` в корені репо. Логіка інструментів
живе в `backend/analysis/services/mcp_api/`; тут лише транспорт
(`docker compose exec -T web manage.py mcp_rpc`) і дії, яким потрібен сам docker.

Повна документація, каталог інструментів, цілі (локально/прод) і граблі —
**`docs/mcp-server.md`**.
