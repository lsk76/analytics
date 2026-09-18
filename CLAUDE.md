# tg-event-analytics

**Перед будь-якою роботою прочитай `docs/AI-GUIDE.md`** — точка входу для ШІ:
модель даних, інваріанти, конвеєри, адмінка, мапа файлів, граблі.

Найкоротше, що треба тримати в голові завжди:

- **Event — єдина одиниця аналітики.** Графіки/матриця читають лише Event.
  Інциденти: N постів → 1 подія (дедуп). Критика (monitor): 1 коментар → 1 подія,
  **БЕЗ дедупу** (лічильник «скільки людей» священний); дзеркало —
  `monitor_stages.sync_comment_event`.
- **Агрегації — тільки через `analysis/services/metrics.py`** (EventSource/PostSource);
  перед `.values().annotate()` завжди `.order_by()`.
- **Гео — `region_subject` FK**, не сирий текст `region` («Саха» матчить «Сахалін»).
- **Нова категорія тегів ⇒ створити рядок `TagCategory`**, інакше адмін-фасет ламається (`?e=1`).
- **Налаштування, які має правити оператор без деплою (промпти, тексти, прапорці) ⇒
  key-value таблиця `Setting`**: додати рядок (`/admin/analysis/setting/`) і читати з коду
  `Setting.get("ключ", ДЕФОЛТ)` (порожнє значення = дефолт із коду). Не хардкодити такий
  конфіг. Приклад: `digest_report_prompt` (промпт дайджест-звіту, `services/infospace/report.py`).
- **Керування сервісом із чату — MCP-сервер** (`mcp_server/`, док `docs/mcp-server.md`):
  `service_health` (що стоїть і хто розгрібає), `accounts_list`/`account_check`,
  `chats_list`, `sources_list`, `run_create`, `service_restart`… Нові інструменти
  додавай У DJANGO-ШАР `backend/analysis/services/mcp_api/` — host-сервер бере їх
  із маніфесту сам, дублювати в `mcp_server/server.py` не треба.
- **Конвеєри НЕ вигадувати** — вони задокументовані: `docs/comments-analysis-pipeline.md`
  (критика), `docs/ethnic-events-pipeline.md` / `docs/econ-events-pipeline.md` (ad-hoc події),
  `docs/ARCHITECTURE.md` (events-воркери). Промпти monitor — `analysis/pilot/prompts.py`.
- **TeleZip:** повний API (exact/regex/фільтр по опису каналу й автору, статистика
  без викачування, пошук каналів і юзерів) — `docs/telezip-api.md`, з чату `tz_*`.
  Глибина індексу ~352 дні: старіше не шукається взагалі.
- **TeleZip:** без негації в запитах; збір по днях/по одному каналу; після падіння VPN —
  `echo "77.88.192.66 api.telezip.net" >> /etc/hosts` у контейнері.
- Зміни коду воркерів/адмінки → `docker compose restart worker-…` / `web`.
- Перед схемними змінами БД — бекап `pg_dump` у `backups/`.
- Адмінка: http://localhost:8001/admin/ (події: `#charts` вбудовані графіки, `matrix/` —
  матриця 8 республік; дефолт списку подій — лише «Схвалено», `?review_status=all` — все).
