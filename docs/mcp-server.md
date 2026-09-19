# MCP-сервер керування сервісом

> Локальний MCP-сервер, через який ШІ-асистент (Claude Code / Desktop) керує
> живим сервісом: Telegram-акаунти й проксі, моніторинги, збори, черги
> конвеєрів, публікація. Стан на 2026-09-18.

## 1. Навіщо

Щоденна експлуатація — це не код, а питання «що стоїть і чому»: чи живий акаунт,
чи не обмежив його SpamBot, чому джерело мовчить третій тиждень, скільки постів
висить на стадії й хто їх має розгрібати. Раніше на кожне таке питання йшов
ручний `docker compose exec … manage.py shell -c "…"` — довго й щоразу заново.

Тепер це інструменти MCP: асистент викликає `service_health`, `accounts_list`,
`run_create` тощо, а не вигадує разові скрипти проти живої БД.

## 2. Архітектура (де що живе)

```
Claude Code ──stdio──> mcp_server/server.py ──docker compose exec -T──>
    manage.py mcp_rpc <tool> <──JSON у stdin──  analysis/services/mcp_api/*
```

- **`backend/analysis/services/mcp_api/`** — УСЯ предметна логіка: реєстр
  інструментів, резолви посилань, форматери; `telezip.py` — повний пошуковий
  API TeleZip (контракт — `docs/telezip-api.md`). Хендлер повертає ГОТОВИЙ ТЕКСТ
  (таблицю), бо це кінцева відповідь моделі, а не проміжна структура.
- **`backend/analysis/management/commands/mcp_rpc.py`** — транспорт у контейнер:
  читає JSON зі stdin, друкує результат між маркерами `<<<MCP-RESULT-*>>>`
  (щоб випадковий `print` стороннього коду не зламав розбір).
- **`mcp_server/server.py`** — host-процес: НЕ дублює логіку, а **генерує
  інструменти з маніфесту** (`mcp_rpc --list`), тож новий інструмент у Django
  з'являється в MCP сам. Свої тільки ті, яким потрібен docker, а не БД:
  `service_ps`, `service_logs`, `service_restart`, `worker_once`.
- **`mcp_server/tools.json`** — знімок маніфесту: якщо стек лежить, сервер усе
  одно стартує зі списком інструментів (і чесно скаже про помилку при виклику).

**Чому все виконується в контейнері, а не з хоста.** Воркери, Telethon-сесії,
проксі й ключі живуть там. Перевірка «акаунт живий / проксі жива», зроблена з
ноутбука, відповідала б на інше питання, ніж те, яке ставить оператор.

## 3. Встановлення

```bash
uv venv mcp_server/.venv --python 3.11
VIRTUAL_ENV=mcp_server/.venv uv pip install -r mcp_server/requirements.txt
```

Реєстрація для Claude Code вже в репо — `.mcp.json` (project scope). Якщо
проєкт лежить в іншому каталозі, задай `TGA_HOME`.

Перевірка без ШІ:

```bash
docker compose exec -T web python manage.py mcp_rpc --list        # усі інструменти
docker compose exec -T web python manage.py mcp_rpc service_health --raw
echo '{"ref":"3"}' | docker compose exec -T web python manage.py mcp_rpc account_show --raw
```

## 4. Цілі керування (локально / прод)

Ціль задається змінними середовища процесу MCP:

| змінна | дефолт | що робить |
|--------|--------|-----------|
| `TGA_DIR` | корінь репо (або `/opt/tg-event-analytics` для ssh) | каталог з compose-файлами |
| `TGA_COMPOSE_FILES` | `docker-compose.yml` | набір файлів через `:` |
| `TGA_SSH` | — | хост із `~/.ssh/config`; усе піде через ssh |
| `TGA_WEB_SERVICE` | `web` | сервіс, у якому виконується `manage.py` |
| `TGA_READONLY` | — | `1` — інструменти, що пишуть, відмовляють (і на хості, і в контейнері) |
| `TGA_TIMEOUT` | `240` | таймаут одного виклику, секунд |

Прод аналітики (`analytics.matter-d.pro`) — стек `DC_LIVE` (базовий +
monitor-компоуз, див. [[analytics-prod-server]] у пам'яті) — уже зареєстрований
у `.mcp.json` другим сервером **у режимі лише-читання** (рішення власника
2026-09-18): дивитись стан можна, змінювати — ні. Обидва шари відмовляють:
host — на docker-діях, контейнер — на інструментах із поміткою «пише». Щоб
тимчасово дозволити зміни, прибери рядок `TGA_READONLY`:

```json
"tg-analytics-prod": {
  "command": "/шлях/до/репо/mcp_server/.venv/bin/python",
  "args": ["/шлях/до/репо/mcp_server/server.py"],
  "env": {
    "TGA_SSH": "tg-analytics",
    "TGA_DIR": "/opt/tg-event-analytics",
    "TGA_COMPOSE_FILES": "docker-compose.yml:docker-compose.monitor.yml",
    "TGA_READONLY": "1",
    "TGA_TIMEOUT": "300"
  }
}
```

Деплой на прод: `git push` → на сервері `git fetch origin main &&
git merge --ff-only origin/main` (у гілки немає upstream, голий `pull` падає) →
`make live-restart-web`. Образ перезбирати НЕ треба: `./backend` змонтований, а
host-залежності MCP (`mcp_server/`) на сервер не їдуть — там працює лише
`manage.py mcp_rpc`.

## 4a. Мережевий режим: доступ для інших людей (прод)

Локальний сервер — це stdio-процес на машині власника: жодного порту, жодної
автентифікації, повні права. Для кількох людей цього замало, тож на проді
працює ДРУГИЙ режим того самого шару інструментів.

```
ноутбук власника:  Claude → stdio → mcp_server/server.py → docker exec → mcp_rpc → mcp_api
прод, інші люди:   клієнт → HTTPS → nginx → процес `mcp` (web-образ) → mcp_api
```

Ключова різниця: у мережевому режимі виклик іде **від імені Django-користувача**
з OAuth-токена, а не «від машини».

### Що бачить і що може користувач

| роль | скоупи | що дає |
|------|--------|--------|
| `reader` | `mcp:read` | лише перегляд стану |
| `operator` | `+ mcp:write` | збори, правки чатів/джерел/акаунтів, `tz_ingest` |
| `admin` | `+ mcp:admin` | налаштування (`setting_set`), слоти TeleZip, `tz_probe` |

**Видимість даних — рівно як в адмінці:** свої задачі (`owner`), свої або спільні
Telegram-акаунти (`visible_to`), джерела — ті, що живлять твої задачі. Суперюзер
бачить усе. Чужу задачу не дістати навіть за прямим id — інструмент відповість
«немає», не зізнаючись, що вона існує.

**Секрети маскуються:** пароль проксі видно лише адміну — вивід інструмента
потрапляє в чат і їде до провайдера моделі.

**Docker-інструментів у мережевому режимі НЕМА** (`service_ps`, `service_logs`,
`service_restart`, `worker_once`): сокет усередину не прокидається, тож рестарт
воркерів лишається локальною операцією власника.

### Як дати людині доступ

1. Завести їй звичайного користувача Django (`/admin/auth/user/`) — або взяти
   наявного; `is_staff` потрібен лише щоб увійти через форму адмінки.
2. Видати роль: **Доступ до MCP → Ролі у MCP → Додати** (користувач + роль).
   Немає рядка — немає доступу взагалі, навіть із валідним токеном.
3. Дати адресу сервера: `https://analytics.matter-d.pro/mcp`. Клієнт
   зареєструється сам (RFC 7591) і відкриє браузер на логін+згоду.
4. Людина входить своїм акаунтом, бачить сторінку згоди («клієнт X просить
   права Y») і підтверджує. Клієнт отримує токен на 8 годин + refresh на 30 діб.

Приклад запису в `.mcp.json` користувача:

```json
"tg-analytics": { "type": "http", "url": "https://analytics.matter-d.pro/mcp" }
```

### Відкликати доступ

* **разово** — Доступ до MCP → Токени MCP → дія «Відкликати»;
* **повністю** — зняти галочку на `McpRole.is_active`: перевіряється на КОЖНОМУ
  виклику, тож уже видані токени вмирають одразу, не чекаючи строку.

### Слід

Кожен виклик пишеться в **Доступ до MCP → Виклики MCP**: хто, який інструмент,
з якими параметрами, успіх/помилка, тривалість. Відмови доступу теж — інакше
«хто вимкнув джерело» лишається без відповіді. Локальні stdio-виклики власника
не журналюються (це його машина, шуму більше за користь).

### Безпека токенів

У базі лежать лише `sha256` — ні токен, ні код авторизації, ні client_secret
не зберігаються у відкритому вигляді. Refresh ротується: після обміну старий
вмирає одразу, тож украдений не проживе 30 діб. Скоупи ніколи не перевищують
роль: клієнт може попросити `mcp:admin`, читач отримає `mcp:read`.

## 5. Каталог інструментів

Позначка **[пише]** = інструмент змінює стан (БД, Telegram, контейнери).
Актуальний список завжди у `mcp_rpc --list` / `tools_manifest`.

### Сервіс (docker, host-шар)

| інструмент | що робить | параметри |
|------------|-----------|-----------|
| `service_ps` | контейнери: статус, аптайм, рестарт-лупи | — |
| `service_logs` | логи сервісу з фільтром | service, lines=80, grep='', since='' |
| `service_restart` **[пише]** | рестарт; `recreate=true` — пересоздання (обов'язкове після зміни `.env`) | services, recreate=False |
| `worker_once` **[пише]** | один прохід стадії (`run_worker --stage X --once`) | stage, task='', timeout=600 |

### Сервіс (дані)

| інструмент | що робить | параметри |
|------------|-----------|-----------|
| `service_health` | стан сервісу одним екраном: черги, збори, акаунти, джерела, публікація | — |
| `service_queues` | черги детально: стадії×задачі, застряглі claim'и, свіжі помилки | task='', stage='', errors=3 |
| `settings_list` | key-value налаштування (`Setting`) | prefix='' |
| `setting_set` **[пише]** | записати налаштування | key, value, description='' |
| `publish_status` | профілі публікації + останні публікації | limit=10 |
| `tools_manifest` | список інструментів шару | — |

### Telegram-акаунти

| інструмент | що робить | параметри |
|------------|-----------|-----------|
| `accounts_list` | акаунти: авторизація, SpamBot, проксі, навантаження | query='', problems_only=False, limit=100 |
| `account_show` | картка акаунта: конфіг, що обслуговує, завдання, боти | ref |
| `account_check` | жива перевірка (connect+get_me через проксі) | ref, pause=2.0 |
| `account_spam_check` **[пише]** | статус через @SpamBot | ref, pause=2.0 |
| `account_update` **[пише]** | активність / проксі / теги | ref, is_active, proxy, add_tags, remove_tags |
| `account_warm_up` **[пише]** | у чергу прогріву (підписка на канали) | ref, channels=0 |
| `account_dialogs` | на що акаунт підписаний (наживо) | ref, limit=40, kind='' |
| `account_jobs` | черги `warm_up` / `test_bot` | kind='all', status='', limit=20 |
| `proxies_list` | пул проксі | problems_only=False, limit=60 |
| `proxy_check` **[пише]** | перевірка + авторемонт sticky-сесії | ref, repair=True |

`ref` розуміє `7`, `#7`, номер телефону, частину назви, а для групових —
`all` / `active` / `problem` (для проксі — `all` / `broken`).

### TeleZip (повний пошуковий API — `docs/telezip-api.md`)

| інструмент | що робить | параметри |
|------------|-----------|-----------|
| `tz_status` | мережа, ключ, **глибина індексу й лаг**, слоти, свіжість збору | deep=True |
| `tz_syntax` | шпаргалка: режими запиту, фільтри, оператори, ліміти | — |
| `tz_stats` | скільки цього є + динаміка, БЕЗ викачування повідомлень | query, exact, regex, channel_term, channels, users, days/дати, languages, tags, has_media, source, thread, by=day\|hour |
| `tz_search` | разовий пошук: усі режими й фільтри, ліміт/семпл/сторінки | ті самі + unique, limit, sample, page_size, page_token, samples, chars |
| `tz_calibrate` | обсяг, частка репостів, ризик відлупу ПЕРЕД збором | query…, days=3, project_days=30 |
| `tz_channel_posts` | усе з одного каналу за період (`*` + фільтр каналу) | channel, days/дати, query='*', thread, limit |
| `tz_channels` | пошук КАНАЛІВ за назвою/описом — «хто пише про X» | term, title, about, names, source, page_size, page_token |
| `tz_channel` | картка каналу + чи він є в нашому довіднику | ref |
| `tz_user` | автор: @ім'я/id → профіль, за бажанням його дописи | ref, term, is_bot, is_active, posts_days |
| `tz_context` | N повідомлень до/після знайденого (аудит контексту) | channel, message_id, before, after, anchor_date |
| `tz_macros` | серверні макроси `##ім'я` → готові підзапити | filter, limit |
| `tz_ingest` **[пише]** | записати результат пошуку в задачу (dry_run=true за замовчуванням) | task, query, days/дати, channels, languages, unique, dry_run |
| `tz_slots_set` **[пише]** | глобальний ліміт паралельних запитів (наживо) | count 1..8 |
| `tz_probe` **[пише]** | сирий виклик будь-якого ендпоінта — розвідка API | endpoint, method, params, body |

### Моніторинги

| інструмент | що робить | параметри |
|------------|-----------|-----------|
| `tasks_list` | задачі: конвеєр, обсяги, що підключено | pipeline='', active_only=False |
| `task_update` **[пише]** | параметри збору задачі: запит TeleZip, мови, unique, чанк | ref, telezip_query, languages, unique, chunk_days, is_active, min_subscribers, llm_model |
| `task_show` | картка моніторингу (конфіг стадій, черги, події, збори) | ref |
| `runs_list` | збори: статус, період, прогрес чанків | task='', status='', limit=15 |
| `run_show` | збір детально (аналог «Збори → Статус») | run_id |
| `run_create` **[пише]** | запустити збір за період (планує чанки) | task, date_from, date_to, chunk_days=0, title='' |
| `run_cancel` **[пише]** | скасувати збір + прибрати чанки в черзі | run_id, drop_pending_chunks=True |
| `chats_list` | whitelist чатів: акаунт, режим, свіжість | task='', active, stream_only, problems_only, limit=60 |
| `chat_update` **[пише]** | активність / стрім / акаунт / пріоритет | chat, is_active, stream_enabled, account, priority, forward_media |
| `sources_list` | джерела інформпростору: розклад, health, якість | task='', kind='', problems_only=False, limit=60 |
| `source_update` **[пише]** | активність / інтервал / «опитати зараз» / скид курсора | ref, is_active, poll_interval_sec, poll_now, reset_cursor, account |
| `events_stats` | зріз подій: day/week/month/region/tag:&lt;кат&gt;/task | task, days=14, group_by='day', region, limit=20, review_status='approved' |
| `channels_find` | знайти канал/чат у довіднику | query, limit=20 |

## 6. Типові сценарії

```
«що з сервісом?»            service_health → service_queues(task=…) → service_logs(worker-…)
«чому монітор мовчить?»     task_show → chats_list(problems_only) → account_check(ref=…)
«акаунт не резолвить»       account_spam_check → (limited?) account_warm_up → account_jobs
«джерело не оновлюється»    sources_list(problems_only) → source_update(poll_now) → worker_once(info_collect)
«зібрати період»            run_create → run_show → (ready) events_stats
«поміняти промпт»           settings_list → setting_set → service_restart(worker-…)
«новий запит до TeleZip»    tz_syntax → tz_stats → tz_calibrate → tz_search
                            → task_update → run_create
«хто пише про тему»         tz_channels(term=…) → tz_channel → chats_list/chat_update
«хто автор коментаря»       tz_user(ref=@…) / tz_context(channel, message_id)
«TeleZip мовчить»           tz_status (мережа/глибина/лаг) → tz_slots_set
```

## 7. Граблі

- **`service_restart` НЕ перечитує `.env`.** Після зміни ключів —
  `service_restart(services=…, recreate=true)`, інакше воркер житиме зі старим
  оточенням (ловили 401 добу).
- **Мережеві інструменти повільні за визначенням** (5-20с на акаунт), тому в
  них є стелі на розмір вибірки; `TGA_TIMEOUT` за замовчуванням 240с.
- **Агрегати рахуються одним згрупованим запитом, а не «останній рядок задачі»
  в циклі:** на мільйонах постів зворотний скан pk-індексу під фільтром задачі
  коштував ~3с НА ЗАДАЧУ (45с на екран `service_health`). Якщо додаєш зріз —
  перевір план, а перед `.values().annotate()` не забувай `.order_by()`
  (інваріант §1.5 AI-GUIDE).
- **`mcp_rpc` не читає stdin наосліп.** Host шле параметри `--payload`, а руками
  можна й піпою (`echo '{…}' | … mcp_rpc account_show`). Раніше команда без піпи
  чекала EOF на відкритому stdin — у фоновій оболонці це зависання назавжди
  (ловили на 11.5 годин).
- **Кожен запит до TeleZip ≈ $0.10** (за виклик, не за обсяг). `tz_stats` — 1
  виклик, `tz_calibrate` — 2, кожна сторінка пошуку — ще один; збір = 1 запит на
  чанк, тому `run_create` показує оцінку ціни до запуску. Деталі —
  `docs/telezip-api.md` §4.
- **TeleZip має ГЛИБИНУ пошуку** (`tz_status` → `searchDateLimit`): старіше за
  неї не шукається взагалі — порожня відповідь, а не помилка. Вікно повзе, тож
  зібране торік перезібрати вже не вийде.
- **Перед широким пошуком — `tz_stats`**: він рахує на боці TeleZip і не тягне
  повідомлення; `tz_search` на широкому вікні ловить «відлуп» (межі — `tz_syntax`).
- **Нові інструменти пиши в Django-шарі**, не в `server.py`: host бере їх із
  маніфесту автоматично. Докстрінг першим абзацом — це опис, який бачить
  модель; параметри анотуй типами (`str`/`int`/`float`/`bool`) — з них
  будується JSON-схема.
- **Знімок `tools.json` оновлюється сам** при кожному успішному старті сервера;
  комітити його варто разом з новими інструментами.

## 8. Тести

`backend/analysis/tests/test_mcp_api.py` (20 тестів): реєстр і манифест,
резолви посилань, ідемпотентність `run_create`, скасування збору, правки
чатів/джерел/налаштувань, рендер без мережі. Запуск:

```bash
docker compose exec -T web pytest analysis/tests/test_mcp_api.py
```
