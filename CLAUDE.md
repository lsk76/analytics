# tg-event-analytics

**Перед будь-якою роботою прочитай `docs/AI-GUIDE.md`** — точка входу для ШІ:
модель даних, інваріанти, конвеєри, адмінка, мапа файлів, граблі.

Найкоротше, що треба тримати в голові завжди:

- **Event — єдина одиниця аналітики.** Графіки читають лише Event.
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
- **Telegram-акаунти — ТІЛЬКИ через `accounts.services.registry` → `ManagedAccount`**
  (HTTP до процесу `tg-gateway`, де живе весь Telethon; `docs/tg-gateway-plan.md`).
  Ніякого `TelegramClient` поза `accounts/gateway/` (тест `test_no_direct_telethon`),
  без проксі акаунт не працює, стан/паузи/ремонт проксі веде gateway.
- **Керування сервісом із чату — MCP-сервер** (`mcp_server/`, док `docs/mcp-server.md`):
  `service_health` (що стоїть і хто розгрібає), `accounts_list`/`account_check`,
  `chats_list`, `sources_list`, `run_create`, `service_restart`… Нові інструменти
  додавай У DJANGO-ШАР `backend/analysis/services/mcp_api/` — host-сервер бере їх
  із маніфесту сам, дублювати в `mcp_server/server.py` не треба.
- **Доступ інших людей до MCP — лише через мережевий режим на проді**
  (`manage.py run_mcp_server`, застосунок `mcpauth`): OAuth + Django-юзери +
  ролі (reader/operator/admin) + аудит. Видимість даних там ТАКА САМА, як в
  адмінці (`visible_to`/`owner`) — мережевий MCP не має ставати її обходом.
  Локальний stdio лишається без обмежень (це машина власника).
- **Конвеєри НЕ вигадувати** — вони задокументовані: `docs/comments-analysis-pipeline.md`
  (критика), `docs/ethnic-events-pipeline.md` / `docs/econ-events-pipeline.md` (ad-hoc події),
  `docs/ARCHITECTURE.md` (events-воркери). Промпти monitor — `analysis/pilot/prompts.py`.
- **TeleZip у MCP — рівно три ендпоінти:** `tz_find` (/FIND, `stats=true` —
  лічильники без викачування), `tz_channels` (/CHANNELS), `tz_users` (/USERS)
  + `tz_status` (діагностика). Параметри названі як у боті: `text`, `exact`,
  `channeltext`, `channel`, `user`, `lang`. **Діалекти протилежні: у `tz_*` (v4)
  пробіл = І, а АБО — це `|`; у запиті задачі (`telezip_query`, збір через
  `run_create`, v3) пробіл = АБО, а І — це `+`. Чужий діалект не падає з
  помилкою, а тихо віддає 0 збігів.**
  Нових обгорток НЕ вигадувати. Контракт — `docs/telezip-api.md`.
  Глибина індексу ~352 дні: старіше не шукається взагалі.
- **ОДИН ЗАПИТ TeleZip ≈ $0.10** — платиться за виклик, не за обсяг. Тому обсяг
  питай `tz_find(stats=true)` (1 виклик), а не пошуком; `collect_chunk_days` — прямий
  множник ціни збору (30 днів по дню = $3, по 3 дні = $1); важке вікно
  ділиться навпіл і кожна половина оплачується окремо.
- **TeleZip:** без негації в запитах; збір по днях/по одному каналу; після падіння VPN —
  `echo "77.88.192.66 api.telezip.net" >> /etc/hosts` у контейнері.
- Зміни коду воркерів/адмінки → `docker compose restart worker-…` / `web`.
- Перед схемними змінами БД — бекап `pg_dump` у `backups/`.
- Адмінка: http://localhost:8001/admin/ (стартова = список досліджень; події: `#charts`
  вбудовані графіки; дефолт списку подій — лише «Схвалено», `?review_status=all` — все).
