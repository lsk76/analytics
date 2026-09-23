# MCP-сервер керування сервісом

> MCP-сервер, через який ШІ-асистент (Claude Code / Desktop) керує живим
> сервісом: Telegram-акаунти й проксі, моніторинги, збори, черги конвеєрів,
> публікація. Два режими одного шару інструментів: локальний stdio (§4) і
> мережевий HTTPS+OAuth на проді (§4a). Стан на 2026-09-19.

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
| `TGA_SSH` | — | хост із `~/.ssh/config`; усе піде через ssh. `local` або `-` = без ssh (на самому сервері `.mcp.json` бере його з `TGA_PROD_SSH=local`) |
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

**Якщо деплой зачепив сам шар інструментів** (`analysis/services/mcp_api/`,
інструкції сервера) — `make live-restart-web` НЕ досить: мережевий режим живе в
окремому довгому процесі `mcp`, який тримає реєстр інструментів у памʼяті з
моменту старту. Тобто локальний stdio вже бачить новий набір, а всі, хто ходить
через `https://analytics.matter-d.pro/mcp`, ще працюють зі старим:

```bash
make live-restart-mcp      # $(DC_LIVE) restart mcp
```

Перевірка, що процес піднявся з правильним набором (число має збігатися з
`mcp_rpc --list`):

```bash
make live-logs-mcp | grep '\[mcp\]'    # [mcp] 33 інструментів; issuer https://…
```

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

**Окремих «ролей MCP» немає.** Що людині можна через асистента — те саме, що
їй можна в адмінці: скоупи токена ВИВОДЯТЬСЯ з її прав Django
(`policy.scopes_for`), щоб не тримати дві правди про один доступ.

| дія в праві (`analysis`/`accounts`) | скоуп |
|---|---|
| `view_*` | `mcp:read` |
| `change_*`, `delete_*` | `mcp:write` |
| `add_*` | `mcp:create` |
| суперюзер або `analysis.change_setting` | `mcp:admin` |

`mcp:admin` стоїть окремо, бо ним закрито те, чого з конкретного права не
вивести: глобальні налаштування (`setting_set`) і надсилання повідомлень від
імені акаунта (`tg_send`, `tg_forward`, `tg_poll`, `tg_send_file`,
`tg_create_chat`, `tg_invite`, `tg_contact_add`) — спам-ризик для акаунта.

**Рядок «Доступи до MCP» (`/admin/mcpauth/mcprole/`) лишається, але вирішує
інше:**

* **чи пускати взагалі** — мережевий MCP це окрема поверхня, тож допуск
  видають явно: немає рядка (чи знято «Активний») — доступу немає, хоч би
  яких прав в адмінці людина мала;
* **«Стеля доступу»** (`max_scope`) — необовʼязкове ЗВУЖЕННЯ: «в адмінці
  редагує, а через MCP хай лише читає». Розширити нею не можна;
* **добова квота TeleZip** і лічильник витрати.

**Права розділів** перевіряє кожен інструмент окремо (`mcp_api/perms.py`:
`sources_list` → `analysis.view_source`, `event_update` → `change_event`,
`tg_send` → `accounts.change_telegramaccount`…) — тим самим правом, що пускає
в розділ адмінки. **Видимість рядків** — третій шар, один на систему:
`analysis/services/access.py` (`owner` / `visible_to`), звідки його беруть і
адмінка (`ScopedAdminMixin`), і MCP (`mcp_api.common.scope`).

Скоупи видають права, а не запит клієнта: Claude реєструється з мінімальним
`mcp:read` (у його запиті не має світитися `mcp:admin`) і повторює цей рядок
при авторизації — стелю це не зрізає. Свідомо ВУЖЧИЙ за виданий запит
поважається.

**Docker-інструментів у мережевому режимі НЕМА** (`service_ps`, `service_logs`,
`service_restart`, `worker_once`): сокет усередину не прокидається, тож рестарт
воркерів лишається локальною операцією власника.

### Як дати людині доступ

1. Завести їй звичайного користувача Django (`/admin/auth/user/`) — або взяти
   наявного; **`is_staff` обов'язковий**: логін до сторінки згоди йде формою
   адмінки (`LOGIN_URL = "/admin/login/"`), а вона пускає лише staff. Прав на
   моделі при цьому давати не треба — адмінка в такого користувача порожня.
2. Дати права розділів **групою** (`/admin/auth/user/`): «Telegram-акаунти»,
   «Аналітик джерел і подій», «Публікації», «Продвинутий аналітик» — з них і
   вийдуть скоупи MCP.
3. Допустити до MCP: **Доступ до MCP → Доступи до MCP → Додати** (досить
   вибрати користувача; «Стеля доступу» — лише якщо треба дати через MCP
   МЕНШЕ, ніж в адмінці). Немає рядка — немає доступу, навіть із валідним
   токеном і повними правами.
3. Дати адресу сервера: **`https://analytics.matter-d.pro/mcp`** (працює з
   2026-09-19). Клієнт
   зареєструється сам (RFC 7591) і відкриє браузер на логін+згоду.
4. Людина входить своїм акаунтом, бачить сторінку згоди («клієнт X просить
   права Y») і підтверджує. Клієнт отримує токен на 8 годин + refresh на 30 діб.

**Роль `reader` — це не «лише перегляд свого»:** видимість ріже задачі (`owner`)
й акаунти (`visible_to`), але TeleZip не скоупиться взагалі — читач має весь
пошуковий API (`tz_find`, `tz_channels`, `tz_users`), тобто може витрачати
твої ~$0.10 за виклик. І акаунти **без власника вважаються спільними**, тож
видні будь-кому з роллю: якщо цього не треба — постав їм `user` у
`/admin/accounts/telegramaccount/`.

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

### Як це розгорнуто

Окремий compose-сервіс `mcp` (той самий web-образ, команда `run_mcp_server`)
слухає `127.0.0.1:8765`; назовні пускає nginx із TLS. Маршрути розведені так:
`/mcp` (exact) і OAuth-ендпоінти → MCP-процес, `/mcp/consent/` і все інше →
Django. **Блок має жити в серверi :443**: у :80 він і марний, і шкідливий
(MCP без TLS) — на цьому легко спіткнутись, бо анкер `location / {` є в обох.

Пакет `mcp` потрібен ЛИШЕ процесу mcp; Django-веб і воркери працюють без нього
(тому додавання залежності вимагало перезбірки образу — після неї обов'язково
перевір `docker compose ps` і логи на рестарт-луп: свіжі версії незапінених
пакетів уже одного разу клали стек).

### Після рестарту клієнт ще НЕ знає про зміни

Список інструментів клієнт отримує ОДИН раз — на зʼєднанні, — і далі тримає
його в себе. Тому після зміни набору (додали/прибрали інструмент, перейменували
параметр) сервер і клієнт розʼїжджаються, причому мовчки:

* прибраний інструмент клієнт усе ще показує моделі, а виклик впирається в
  `Unknown tool: X` на сервері;
* новий інструмент моделі просто не видно — вона його не покличе, бо не знає;
* схема параметрів застаріла: виклик із параметром, який сервер уже
  перейменував, поїде як є (ловили `tz_channels(names=…)` замість `name=`).

Діагностика на місці: `tools_manifest` виконується на СЕРВЕРІ й показує живий
набір, а список тулів у клієнта — кешований. Розбіжність між ними означає рівно
це. Звірити з процесом можна по його стартовому рядку (`make live-logs-mcp`).

Лікує лише перепідключення на боці клієнта: у Claude Code — `/mcp` → Reconnect
для цього конектора, у Desktop — Settings → Connectors → вимкнути/увімкнути.
Рестарт серверного процесу сам по собі клієнтам нічого не переписує.

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
| `settings_list` | key-value налаштування (`Setting`); довгий текст обрізаний | prefix='' |
| `setting_show` | повне значення одного ключа (промпт, прапорець) | key |
| `setting_set` **[пише]** | записати налаштування | key, value, description='' |
| `publish_status` | профілі публікації + останні публікації (зведення) | limit=10 |
| `publish_config_show` | картка профілю публікації: канал, режим, відбір, лічильники | ref |
| `publish_config_create` **[пише, mcp:create]** | новий профіль публікації в Telegram-канал (власник = ти; за замовчуванням неактивний) | name, chat_id, task, bot_token, is_active, review_status, tags, require_tags, exclude_tags, regions, max_age_days, publish_from, raw_mode, ai_model, ai_prompt, max_per_pass, post_as_account, forward_account |
| `publish_config_update` **[пише]** | змінити профіль: увімкнути/вимкнути, канал, відбір (списки замінюються повністю, '-' очищає), режим, AI | ref + ті самі поля |
| `published_list` | журнал опублікованого: коли, профіль, подія, посилання на пост; для відсіяних/збійних — причина | config, task, status='published', days=14, query, limit=30 |
| `published_show` | одна публікація повністю: текст поста, вердикт AI, посилання, помилка | ref |
| `tools_manifest` | список інструментів шару | — |

### Telegram-акаунти

Telegram-акаунти потрібні, щоб читати новини з каналів і чатів. Без живої
сесії (авторизований акаунт із робочою проксі) канал чи чат прочитати не
вийде: саме акаунт тягне пости telegram-джерел інформпростору і повідомлення
whitelist-чатів моніторингу; руками те саме роблять `tg_*`. Пошук по індексу
TeleZip (`tz_*`) акаунта не використовує.

| інструмент | що робить | параметри |
|------------|-----------|-----------|
| `accounts_list` | акаунти: авторизація, SpamBot, проксі, навантаження | query='', problems_only=False, limit=100 |
| `account_show` | картка акаунта: конфіг, що обслуговує, завдання, боти | ref |
| `account_check` | жива перевірка через tg-gateway (get_me сесією акаунта) | ref, pause=2.0 |
| `account_repair` **[пише]** | ремонт через gateway: живість → новий session-id проксі → стан | ref (`problem` = cooldown/needs_proxy) |
| `account_spam_check` **[пише]** | статус через @SpamBot | ref, pause=2.0 |
| `account_update` **[пише]** | активність / проксі / теги | ref, is_active, proxy, add_tags, remove_tags |
| `account_import` **[пише]** | додати акаунт із tdata-експорту (`<phone>.json` + `.session`) — як адмінка «Додати акаунт через файли»; сесія base64 або шлях у контейнері; оператор додає собі, спільний — лише суперюзер | meta_json, session_b64 / session_path, tags, proxy, shared |
| `account_import_batch` **[пише, mcp:create]** | БАГАТО акаунтів: тека або zip у контейнері (`/app/backend/_import/` = `backend/_import/` на сервері) чи `zip_b64` з парами `<phone>.json` + `<phone>.session`; по акаунту на пару, дубль/битий файл не зупиняє решту; `dry_run` показує, що буде; `delete_after` прибирає файли після імпорту | path / zip_b64, tags, proxy, shared, dry_run, delete_after |
| `account_warm_up` **[пише]** | у чергу прогріву (підписка на канали) | ref, channels=0 |
| `account_dialogs` | на що акаунт підписаний (наживо) | ref, limit=40, kind='' |
| `account_jobs` | черги `warm_up` / `test_bot` | kind='all', status='', limit=20 |
| `proxies_list` | пул проксі | problems_only=False, limit=60 |
| `proxy_check` **[пише]** | перевірка проксі РЕАЛЬНОЮ сесією акаунта на ній (= `account_repair`) | ref, repair=True |

`ref` розуміє `7`, `#7`, номер телефону, частину назви, а для групових —
`all` / `active` / `problem` (для проксі — `all` / `broken`).

### TeleZip — три ендпоінти, три інструменти (`docs/telezip-api.md`)

Поверхня повторює API як є: `/FIND`, `/CHANNELS`, `/USERS`. Імена параметрів —
з діалекту бота (`text=`, `exact=`, `channeltext=`, `channel=`, `user=`,
`lang=`), щоб запит із гайда працював тут без перекладу.

| інструмент | ендпоінт | що робить | ключові параметри |
|------------|----------|-----------|-------------------|
| `tz_find` | `/FIND` | пошук у текстах повідомлень; `stats=true` віддає лічильники замість повідомлень (`/FindStats`) | text, exact, regex, channeltext, channel, user, lang, hasmedia, unique, source, thread, days/дати, stats, limit, page_size |
| `tz_channels` | `/CHANNELS` | пошук каналів і чатів у базі TeleZip | term, name, title, about, id, source, page_size |
| `tz_users` | `/USERS` | профілі людей: юзернейм → id, вільний пошук по імені | username, id, term, is_bot, is_active |
| `tz_status` | — | діагностика: маршрут до API, ключ, глибина індексу й лаг, слоти, свіжість збору | deep |

`tz_status` — єдиний, що не є обгорткою ендпоінта: він відповідає на «чому
нічого не працює» (найчастіше — впав VPN до api.telezip.net).

**Головна пастка синтаксису: діалект залежить від версії API.** Ці інструменти
ходять у v4, де пробіл — це І, а АБО пишеться `|`: `text="мигрант драка"` вимагає
обидва слова, а треба `text="(мигрант | приезж*) (драка | избил)"`. У запиті
задачі (`task.telezip_query` → збір `run_create` → v3 `/Find`) усе навпаки:
пробіл = АБО, І = `+`. Запит, перенесений з одного діалекту в інший без
переписування, не падає з помилкою — він тихо віддає 0 збігів і все одно коштує
$0.10. Решта операторів і межі — в описі `tz_find` та в `docs/telezip-api.md`
(розділ «Оператори»).

### Telegram «руками» акаунтів — `tg_*` (43 інструменти)

Асистент працює з Telegram від імені наших акаунтів: усе йде `mcp_api/telegram.py`
→ `ManagedAccount.tg(op)` → gateway `accounts/gateway/_tg_tools.py` (єдине місце
з Telethon для цих операцій), тією ж проксі й сесією, що й воркери. Акаунт —
параметр `account` (id/номер/назва, лише видимий викликачу); порожньо = перший
доступний. Покриття — за мотивами chigwell/telegram-mcp.

| група | інструменти |
|-------|-------------|
| читання | `tg_dialogs`, `tg_chat_info`, `tg_history` (пошук/пагінація/від користувача/медіа), `tg_messages` (повний текст, кнопки, реакції), `tg_context`, `tg_search_global`, `tg_message_link`, `tg_download` |
| повідомлення **[пише]** | `tg_edit`, `tg_delete`, `tg_pin`, `tg_mark_read`, `tg_react`, `tg_click` (inline-кнопки), `tg_draft` |
| **надсилання [mcp:admin]** | `tg_send` (reply/markdown/тихо/відкладено), `tg_send_file` (файл/фото/голосове), `tg_forward`, `tg_poll`, `tg_create_chat`, `tg_invite`, `tg_contact_add` |
| участь / адмін **[пише]** | `tg_join`, `tg_leave`, `tg_kick`, `tg_ban`, `tg_restrict` (мут), `tg_admin` (права/титул), `tg_invite_link`, `tg_edit_chat` (назва/опис/slow mode/username); читання: `tg_participants`, `tg_common_chats`, `tg_topics` |
| контакти | `tg_contacts`, `tg_contacts_search` (глобальний пошук людей/чатів); **[пише]** `tg_contact_delete`, `tg_block` |
| профіль | `tg_privacy`; **[пише]** `tg_update_profile`, `tg_set_photo` |
| теки/чернетки | `tg_folders`, `tg_drafts`, `tg_draft` |

Надсилання під `mcp:admin` навмисно: це видимі дії від імені акаунта, за які
прилітає спам-бан. Нової операції не вигадувати в `telegram.py` — додавати в
`_tg_tools.OPS` (gateway) і тонку обгортку в MCP; **після зміни `_tg_tools.py`
треба рестартнути `tg-gateway`** (операції живуть у його процесі), а не лише `mcp`.
Апдейти gateway не читає (`receive_updates=False`), тож «чекати нове
повідомлення» тут нема — читай `tg_history(min_id=…)`.

### Моніторинги

| інструмент | що робить | параметри |
|------------|-----------|-----------|
| `tasks_list` | задачі; колонка «конвеєр» — точний ключ events/monitor/research/infospace/tgsearch | pipeline='', active_only=False |
| `task_create` **[пише]** | створити задачу і одразу поля етапів (ті самі імена, що в task_update / task_show) | slug, name, pipeline + поля форми цього конвеєра |
| `task_update` **[пише]** | будь-яке поле етапу з картки `task_show` (ім'я параметра = ім'я в дужках). Чуже для конвеєра поле відхиляється. Аліаси: `classify_prompt`, `unique`, `chunk_days`, `min_subscribers` | ref + поля форми задачі для її конвеєра |
| `task_show` | картка задачі: зібраний промпт LLM, поля конвеєра, блок «Щоб запустити, бракує» (чати, рубрики, джерела, запит) | ref |
| `prompt_try` **[пише]** | проба скрін-промпта infospace на кількох постах із текстом: вердикт і теги, у БД не пише і чернетку не зберігає. Кожен пост — виклик LLM, стеля 8 | task, limit=3, posts='', days=14, date_from, date_to, only_with_event=true, info_screen_prompt='', info_tagger_prompt='' |
| `posts_retag` **[пише]** | перетегувати вже зібрані події поточним збереженим промптом (лише теги категорій задачі; події не видаляє). confirm=false лише рахує. Стелі 25; повтор із тими самими датами бере ті самі найсвіжіші | task, limit=5, days=14, date_from, date_to, posts='', confirm=false |
| `runs_list` | збори: статус, період, прогрес чанків | task='', status='', limit=15 |
| `run_show` | збір детально (аналог «Збори → Статус») | run_id |
| `run_create` **[пише]** | запустити збір за період (планує чанки) | task, date_from, date_to, chunk_days=0, title='' |
| `run_cancel` **[пише]** | скасувати збір + прибрати чанки в черзі | run_id, drop_pending_chunks=True |
| `chats_list` | whitelist чатів: акаунт, режим, свіжість | task='', active, stream_only, problems_only, limit=60 |
| `chat_add` **[пише]** | додати чат у whitelist monitor/research/tgsearch; невідомий @username створюється в довіднику | task, channel, is_active, stream_enabled, forward_media, account, priority |
| `chat_update` **[пише]** | активність / стрім / акаунт / пріоритет / критичне джерело / нотатка | chat, is_active, stream_enabled, account, priority, forward_media, is_critical_source, notes |
| `chat_delete` **[пише]** | прибрати чат із whitelist (`confirm=true`); зібрані пости лишаються | chat, confirm |
| `rubrics_list` | рубрики research-задачі | task |
| `rubric_create` **[пише]** | рубрика: категорія, тег, ключові слова (усі мають збігтися) | task, tag_category, tag_name, keywords, extra_prompt, is_active, order |
| `rubric_update` **[пише]** | змінити рубрику | ref, tag_category, tag_name, keywords, extra_prompt, is_active, order |
| `rubric_delete` **[пише]** | видалити рубрику (`confirm=true`) | ref, confirm |
| `sources_list` | джерела інформпростору: розклад, health, якість | task='', kind='', problems_only=False, limit=60 |
| `source_update` **[пише]** | активність / інтервал / «опитати зараз» / скид курсора | ref, is_active, poll_interval_sec, poll_now, reset_cursor, account |
| `source_add` **[пише]** | створити джерело за посиланням (`Source.ensure`: рядок довідника + розклад) і одразу підписати задачу | url, kind='', task='', name, region, language, poll_interval_sec, account |
| `source_subscribe` **[пише]** | підписати задачу на джерело / вимкнути підписку / пріоритет | ref, task, active=True, priority=0 |
| `events_stats` | зріз подій: day/week/month/region/tag:&lt;кат&gt;/task | task, days=14, group_by='day', region, limit=20, review_status='approved' |
| `events_list` | список подій із фільтрами адмінки; колонка id — подія, колонка пост — id найранішого поста (для prompt_try/posts_retag). Дефолт — «Схвалено» за 30 дн | task, days=30, date_from, date_to, review_status='approved', region, settlement, tag, query, channel, min_channels, min_reach, order, limit=30 |
| `event_show` | картка події: id, опис, регіон, теги, аудит, пости-джерела з id поста | ref |
| `event_update` **[пише]** | схвалити / відхилити / повернути в чергу (= дії адмінки), теги `кат:тег` (+/−), регіон, нас. пункт, дата, опис, нотатка аудиту | ref, review, notes, add_tags, remove_tags, region, settlement, event_date, summary |
| `event_add` **[пише]** | подія за посиланням (= «Додати подію» в адмінці: fetch → скрін-промпт → Event approved; виклик LLM) | task, url |
| `tag_categories` | категорії тегів (закриті/відкриті) з прикладами — для `tag=` і `add_tags=` | task='' |
| `tag_category_show` | картка категорії: підказка, порядок, задачі | key |
| `tag_category_create` **[пише]** | нова категорія (без рядка фасет `?tag_<ключ>` не реєструється) | key, label, closed=False, hint='', order=100 |
| `tag_category_update` **[пише]** | назва / closed / підказка / порядок (ключ незмінний; closed бачать воркери після рестарту) | key, label, closed, hint, order=-1 |
| `tag_category_delete` **[пише]** | видалити категорію; з тегами чи задачами — лише confirm=true | key, confirm=False |
| `tags_list` | теги довідника | category='', query='', limit=80 |
| `tag_show` | картка тега й аліаси | ref |
| `tag_create` **[пише]** | канонічний тег (і сід закритої категорії); повтор не дублює | category, name |
| `tag_update` **[пише]** | перейменувати або перенести в іншу категорію | ref, name, category |
| `tag_delete` **[пише]** | видалити тег; якщо висить на подіях/постах — лише confirm=true | ref, confirm=False |
| `channels_find` | знайти канал/чат у довіднику | query, limit=20 |
| `channel_add` **[пише]** | додати рядок довідника за посиланням/@username (ідемпотентно; дописує порожні поля й теми) | url, title, region, topics, chat_type, language |
| `channel_update` **[пише]** | теми (теги) +/−, назва, регіон, нас. пункт, тип, фокус | ref, add_topics, remove_topics, title, region, settlement, chat_type, focus, discusses_problems |

## 6. Типові сценарії

```
«що з сервісом?»            service_health → service_queues(task=…) → service_logs(worker-…)
«чому монітор мовчить?»     task_show → chats_list(problems_only) → account_check(ref=…)
«акаунт не резолвить»       account_spam_check → (limited?) account_warm_up → account_jobs
«джерело не оновлюється»    sources_list(problems_only) → source_update(poll_now) → worker_once(info_collect)
«зібрати період»            run_create → run_show → (ready) events_stats
«поміняти промпт»           task_show → prompt_try (чернетка) → task_update
                            → posts_retag(confirm=false) → posts_retag(confirm=true, limit=…)
                            глобальне Setting: settings_list → setting_show → setting_set
«новий запит до TeleZip»    tz_find(stats=true) → tz_find (тексти)
                            → task_update → run_create
«хто пише про тему»         tz_channels(term=…) → chats_list/chat_update
«хто автор коментаря»       tz_users(username=…) → tz_find(user=<id>, text="*")
«TeleZip мовчить»           tz_status (мережа/глибина/лаг); ліміт слотів — в адмінці
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
- **Платні `tz_*` у мережевому режимі впираються в добову квоту користувача**
  (§4a): перевищення — відмова до запиту. Інструмент, що робить платний запит,
  зобов'язаний викликати `registry.charge(n)` ПЕРЕД ним — інакше він не
  лімітується й не рахується (`tz_users` робить два запити — два `charge`).
- **Кожен запит до TeleZip ≈ $0.10** (за виклик, не за обсяг). `tz_find(stats=true)` — 1
  виклик, кожна сторінка пошуку — ще один; збір = 1 запит на
  чанк, тому `run_create` показує оцінку ціни до запуску. Деталі —
  `docs/telezip-api.md` §4.
- **TeleZip має ГЛИБИНУ пошуку** (`tz_status` → `searchDateLimit`): старіше за
  неї не шукається взагалі — порожня відповідь, а не помилка. Вікно повзе, тож
  зібране торік перезібрати вже не вийде.
- **Перед широким пошуком — `tz_find(stats=true)`**: рахує на боці TeleZip і не
  тягне повідомлення; звичайний `tz_find` на широкому вікні ловить «відлуп»
  (межі — в описі інструмента й у `docs/telezip-api.md`). **Але статистика
  розбирає запит як АБО**, тож вірити їй можна лише на чисто-АБО запиті
  (синоніми через `|`); для запиту з І-групами (пробіл між групами) обсяг дає
  тільки сам змет — деталі й цифри в `docs/telezip-api.md` §3.
- **`tz_find` віддає максимум 10 000 за виклик** (`page_size` — максимум 1000).
  Якщо віддано рівно стільки — вибірку ОБРІЗАЛО: ділити вікно навпіл і качати
  половини (дешевше за сторінки: 20 тис. = 4 виклики проти 20).
- **Перетегування infospace — `prompt_try`, потім `posts_retag`.** Повний скид
  постів у `info_collected` (`rescreen_task_now`) видаляє всі події задачі й
  кличе LLM на кожен пост; у MCP його немає. Ретеншн уже вирізав тексти
  нерелевантних done старші за `info_retention_days` — їх перепрогнати нічим.
  `posts_retag` міняє лише теги категорій задачі, стеля 25 подій за виклик.
- **Нові інструменти пиши в Django-шарі**, не в `server.py`: host бере їх із
  маніфесту автоматично. Докстрінг першим абзацом — це опис, який бачить
  модель; параметри анотуй типами (`str`/`int`/`float`/`bool`) — з них
  будується JSON-схема.
- **Знімок `tools.json` оновлюється сам** при кожному успішному старті сервера;
  комітити його варто разом з новими інструментами.
- **Зміна набору інструментів доходить до людей у три кроки, і кожен можна
  забути:** мерж на сервері → рестарт процесу `mcp` (`make live-restart-mcp`,
  бо `live-restart-web` його не чіпає) → перепідключення клієнта (`/mcp` →
  Reconnect). Пропустиш другий — мережеві користувачі лишаються на старому
  наборі; пропустиш третій — те саме, але вже в конкретного клієнта, і виглядає
  як «інструмент зник / `Unknown tool`». Деталі — §4a.

## 8. Тести

`backend/analysis/tests/test_mcp_api.py`: реєстр і манифест, резолви посилань,
ідемпотентність `run_create`, скасування збору, правки чатів/джерел/задач,
довідник каналів, події (список із фільтрами, аудит, теги), рендер без мережі.
`backend/analysis/tests/test_mcp_access.py`: ролі, видимість (задачі, акаунти,
проксі, завдання, події), маскування секретів, квота TeleZip, аудит.
`backend/mcpauth/tests/`: OAuth-флоу. Запуск:

```bash
docker compose exec -T web pytest analysis/tests/test_mcp_api.py analysis/tests/test_mcp_access.py mcpauth/tests
```

Тести OAuth ходять по `http://testserver`, тому `conftest.py` вимикає
`SECURE_SSL_REDIRECT` (інакше 301 замість відповіді).
