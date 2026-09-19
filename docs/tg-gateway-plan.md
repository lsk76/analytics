# Telegram Gateway — план переїзду на централізовану роботу з акаунтами

Статус: **план, погоджено 2026-09-19**. Реалізація локально → тести → одне
вікно cutover на проді (20–30 хв зупинки) → відкат за потреби.

## 1. Проблема

Робота з Telegram-акаунтом розмазана по ~10 споживачах, і кожен сам вирішує,
який акаунт узяти, що робити зі збоєм і як не перетнутись з іншими:

| споживач | файл | як бере акаунт | що робить зі збоєм |
|---|---|---|---|
| збирач infospace ×5 реплік | `analysis/services/infospace/adapters/telegram.py` | пул `(source.id + acc_shift) % N` + advisory lock | пише на **джерело** (експ. бекоф до 6 год); ротує акаунт лише на «No user has…» |
| стрім tgsearch | `analysis/services/tgsearch_stages.py` | `MonitorChat.tg_account` + async advisory lock | відвʼязує чати від акаунта |
| публікація | `analysis/services/publish/stages.py` | `PublishConfig.forward_account` + lock | кидає `TelegramError` |
| warm-up / spam-status / test-bot | `accounts/services/*_stage.py` | прямі `*_sync` без lock | своє |
| proxy_healthcheck | `accounts/services/proxy_health.py` | **без акаунта**, голий `connect()` раз на 4 год | регенерує session-id |
| адмін-дії, REST-логін | `accounts/admin.py`, `accounts/views.py` | прямі `*_sync` без lock | повідомлення в UI |
| MCP `account_check/dialogs/spam_check` | `analysis/services/mcp_api/accounts.py` | прямі `*_sync` без lock | текст у чат |
| research-скрипт | `management/commands/research_task_fedpressure.py` | прямий | — |

Наслідки, які вже бачили на проді:

- **Убиті сесії (`AuthKeyDuplicated`)**: той самий auth key з двох IP. Джерела:
  паралельні клієнти без lock (сервісні воркери, адмінка) і **фолбек без проксі**
  (`Proxy.to_telethon_proxy()` повертає `None`, коли `is_working=False`, і акаунт
  іде в Telegram напряму з IP сервера).
- **Джерела «лежать» через проксі**: збирач бачить `Connection refused`, але карає
  джерело; проксі про це дізнається через 4 год, акаунт — ніколи.
- **Handshake на кожен полінг**: 573 джерела × 5 реплік = тисячі `connect()` на
  годину через sticky-проксі, які «не гарантують IP».
- **Нема observability**: у логах `fetch failed` немає id акаунта і проксі.

## 2. Цільова архітектура

```
 worker-info-collect ×5 ─┐
 worker-tgs-stream       │   HTTP (docker-мережа)     ┌──────────────────────────┐
 worker-publish          ├──── ManagedAccount ───────►│  tg-gateway (1 процес)   │
 worker-warm-up/spam/bot │   (тонкий клієнт)          │  117 живих TelegramClient│
 web (адмінка, REST)     │                            │  asyncio.Lock на акаунт   │
 mcp                     ┘                            │  стейт-машина + ремонт    │
                                                      └─────────┬────────────────┘
                                                                │ ORM: стан → TelegramAccount
                                                                ▼
                                                            Postgres
```

Принципи:

1. **Telethon живе тільки в gateway.** Один процес, один довгоживучий клієнт на
   акаунт, один `asyncio.Lock` на акаунт. Таблиця lease, advisory lock, TTL —
   не потрібні.
2. **Без проксі не працюємо.** Немає робочої проксі → акаунт у стані
   `needs_proxy`, операція кидає `AccountUnavailable`. Фолбек на IP сервера
   видаляється.
3. **Стейт-машина в одному місці** (gateway), стан дублюється в рядок
   `TelegramAccount` для адмінки/MCP/політики вибору.
4. **Споживачі знають лише `ManagedAccount`** і два винятки:
   `RateLimited(seconds)` та `AccountUnavailable`. Локи, проксі, реконекти,
   класифікація помилок — не їхня справа.
5. **Один інстанс на акаунт — справжній**, бо процес один. У воркерах
   `ManagedAccount` — легка ручка (id + HTTP), без стану.

## 3. Компоненти

### 3.1 `accounts/gateway/` (новий пакет, працює тільки в gateway-процесі)

| файл | що |
|---|---|
| `server.py` | aiohttp-застосунок, `manage.py run_tg_gateway`, порт 8010 всередині docker-мережі |
| `pool.py` | `AccountPool`: `{account_id: LiveAccount}`, ліниве підключення при першому виклику, реконект, idle-відключення (Setting `gateway_idle_disconnect_sec`, дефолт 0 = тримати завжди) |
| `live.py` | `LiveAccount`: Telethon-клієнт + `asyncio.Lock` + стейт-машина; всі операції з §4 |
| `state.py` | переходи станів + запис у `TelegramAccount` через `sync_to_async` |
| `repair.py` | фонова корутина: `due_for_check()` → `repair()`; замінює `worker-proxy-health` |
| `_telethon.py` | те, що зараз `TelegramUserClient` (побудова клієнта, device kwargs, операції); **приватний**, імпорт ззовні заборонений тестом |

### 3.2 `accounts/services/managed.py` (клієнт, у всіх процесах)

```python
class ManagedAccount:
    id: int
    # читання Telegram
    scan(chats, patterns=(), media=None) -> list[dict]   # єдина операція читання, §10
    fetch_history(handle, min_id=0, limit=50, reverse=False) -> list[dict]  # = scan одного чату
    resolve(handle) -> dict                     # {id, access_hash}
    dialogs(kind="") -> list[dict]
    recent_messages(peer, limit=20) -> list[dict]
    get_me() -> dict
    # дії
    forward(from_chat, msg_id, to_chat) -> dict
    send_post(to_chat, text, src_chat=None, src_msg_id=0, src_peer=None) -> dict
    join(handles) -> dict
    # сервіс
    check_alive() -> dict
    spam_status() -> dict
    test_bot(bot_username, feedback_text="") -> dict
    botfather(op, **kw) -> dict                 # create/sync/set_name/set_photo
    # авторизація
    send_code() -> dict
    verify_code(code, password=None) -> dict
    # самообслуговування
    repair() -> dict                            # живість → регенерація проксі → стан
    replace_proxy(proxy_id) -> dict             # змінити + підтвердити живість
    # без gateway (лише БД)
    state / cooldown_until / proxy_id / is_available()
```

Кожен метод = один HTTP-виклик `POST /accounts/{id}/{op}` з таймаутом
(`Setting gateway_call_timeout_sec`, дефолт 120). Відповідь `{ok, result}` або
`{ok: false, error: {kind, message, retry_after}}`; `kind ∈ {busy, rate_limited,
unavailable, transport, telegram, internal}` → клієнт кидає відповідний виняток.

**Gateway недоступний** (connection refused/timeout HTTP) → `GatewayDown`,
споживачі трактують як `RateLimited(30)`: не рахують збоєм джерела/чату, не
відвʼязують акаунти.

### 3.3 `accounts/services/registry.py` (фабрика)

```python
registry.get(account_id) -> ManagedAccount           # без перевірки доступності
registry.pick(role, key) -> ManagedAccount           # з пулу за політикою, NoAccountAvailable
registry.pinned_for(obj) -> ManagedAccount | None    # obj: Source / MonitorChat / PublishConfig
```

Політика `pick(role="collector", key=source.id)`:

- кандидати: `is_active`, `is_authenticated`, `state == ready`,
  `cooldown_until < now`, `resolve_exhausted_until < now` (для операцій з резолвом),
  `proxy.is_working`;
- виключити акаунти, привʼязані до несумісних ролей: `stream`
  (`MonitorChat.tg_account`), `publisher` (`PublishConfig.forward_account`);
- стабільний вибір `(key + shift) % N`, `shift` зберігає споживач (як зараз
  `poll_cursor.acc_shift`), збільшує на `AccountUnavailable`/`RateLimited`;
- ролі та виключення — у `Setting` (`registry_roles_json`), щоб оператор міг
  зняти акаунт з ролі без деплою.

Реєстр **не кешує** рядки: кожен виклик читає БД.

### 3.4 Модель `TelegramAccount` — нові поля (міграція адитивна)

| поле | тип | дефолт |
|---|---|---|
| `state` | char: `ready` / `cooldown` / `needs_proxy` / `deauthorized` / `banned` | `ready` |
| `cooldown_until` | datetime null | null |
| `transport_failures` | int | 0 |
| `resolve_exhausted_until` | datetime null | null |
| `last_ok_at` | datetime null | null |
| `last_error` | text | "" |
| `gateway_connected` | bool | false (для адмінки: чи тримає gateway живий клієнт) |

`is_authenticated` лишається (== `state not in {deauthorized, banned}`), щоб не
ламати наявні фільтри; при переході синхронізуємо обидва.

## 4. Стейт-машина (у `LiveAccount`)

| подія | дія | стан |
|---|---|---|
| успішна операція | `last_ok_at=now`, `transport_failures=0` | `ready` |
| transport: `ConnectionError`, `Connection refused`, `TimeoutError`, `Server closed` | `transport_failures++`; проксі `fail_count++`, `last_tested_at=None`; cooldown `5хв × 2^(n-1)` (кап 1 год); при `n ≥ 3` — негайний `repair()` | `cooldown` |
| `repair()` не зміг (3 регенерації session-id) | проксі `is_working=False`; акаунт | `needs_proxy` |
| `FloodWaitError(s)` | `cooldown_until = now + s`; операція кидає `RateLimited(s)` | `cooldown` |
| «No user has … as username» / `UsernameInvalid` / `Cannot find any entity` | `resolve_exhausted_until = now + 24h`; кидає `AccountUnavailable(resolve)` | `ready` (для операцій без резолву) |
| `AuthKeyUnregistered` / `SessionRevoked` / `AuthKeyDuplicated` / `UserDeactivated` | `is_authenticated=False`, клієнт закрито | `deauthorized` |
| `UserDeactivatedBan` / `PhoneNumberBanned` | те саме | `banned` |
| оператор: `replace_proxy` → `check_alive` ok | `transport_failures=0` | `ready` |
| оператор: `verify_code` ok | нова `session_string` | `ready` |

Перехід = запис у БД + рядок у лог `acc=#id proxy=#id op=… → state`.
Cooldown-константи — у `Setting` (`gateway_cooldown_base_sec`, `gateway_cooldown_cap_sec`,
`gateway_repair_after_failures`).

## 5. Що робимо зі споживачами

| споживач | стає |
|---|---|
| `infospace/adapters/telegram.py` | `acc = registry.pinned_for(source) or registry.pick("collector", key=source.id)`; `acc.fetch_history(...)`; `_account()`, `_fetch_history`, `account_exclusive` — видалити. `RateLimited`/`AccountUnavailable` → як зараз `_schedule_rate_limited` + `acc_shift++`. `_is_resolve_error`, `_is_transport_error`, `_flag_proxy_suspect` у `stages.py` — видалити (логіка переїхала в gateway) |
| `tgsearch_stages.py` стрім | `_stream_account` → для кожного чату `registry.get(mc.tg_account_id).fetch_history(...)` (gateway тримає одне зʼєднання, тож «пачка чатів на акаунт» більше не потрібна для економії handshake); `_assign_accounts` → `registry.pick("stream", key=chat.id)`; `_flooded_ids`/`_mark_flood` — видалити, це `cooldown` |
| `publish/stages.py` | `registry.get(config.forward_account_id).send_post(...)`; `account_exclusive` — видалити |
| `warmup_stage.py`, `spam_status_stage.py`, `test_bot_stage.py` | `registry.get(job.account_id).join(...)` / `.spam_status()` / `.test_bot(...)` |
| `proxy_health.py` + `worker-proxy-health` | видалити; заміна — `gateway/repair.py` (цикл раз на `gateway_repair_interval_sec`, дефолт 600, по акаунтах з `state in {cooldown, needs_proxy}` або `last_ok_at` старше 4 год) |
| `accounts/admin.py`, `accounts/views.py` | усі `TelegramUserClient.*_sync` → методи `ManagedAccount`; нова адмін-дія «Полагодити» = `repair()`; зміна проксі у формі → `replace_proxy()` |
| `mcp_api/accounts.py` | `account_check` → `check_alive()`, `account_dialogs` → `dialogs()`, `account_spam_check` → `spam_status()`; новий `account_repair`; `proxy_check` → `repair()` акаунтів цієї проксі |
| `mcp_api/service.py` `service_health` | секція «Gateway»: підключено N/117, у cooldown, needs_proxy, транспортних збоїв за годину |
| `research_task_fedpressure.py` | `registry.get(...).resolve(h)` |
| `telegram_client.py` | стає `accounts/gateway/_telethon.py`; `run_async`, `account_exclusive*`, `AccountBusy` — видалити після переїзду |

Тест-запобіжник: `accounts/tests/test_no_direct_telethon.py` — grep по
`backend/` (крім `accounts/gateway/`): `import telethon`, `TelegramClient(`,
`_telethon` → падає.

## 6. Docker / конфіг

- Новий сервіс у `docker-compose.yml` і `docker-compose.monitor.yml`:
  `tg-gateway: { <<: *worker, command: python manage.py run_tg_gateway, expose: ["8010"] }`,
  без `ports` (тільки внутрішня мережа). `restart: unless-stopped`.
- `TG_GATEWAY_URL=http://tg-gateway:8010` в `x-worker.environment` і у `web`,
  `mcp`. Локальний stdio-MCP (`mcp_server/`) ходить у Django-шар, тож бере
  той самий URL з `.env`.
- Видалити `worker-proxy-health`.
- Graceful shutdown: SIGTERM → перестати приймати, дочекатись операцій
  (≤ 30 с), `disconnect()` усім. `stop_grace_period: 40s`.
- Памʼять: ~117 клієнтів × ~10–15 МБ ≈ 1.5 ГБ; на проді 16 ГБ. Ліміт
  `mem_limit: 3g`.

## 7. Тести

**Unit (без мережі):**
- `state.py`: кожен рядок таблиці §4 — вхід виняток → вихід поля рядка.
- `live.py` з фейковим Telethon (обʼєкт з методами, що кидають задані
  винятки): lock тримається на час операції; паралельний виклик того самого
  акаунта чекає, а не падає; `repair()` → 3 регенерації → `needs_proxy`.
- `registry.pick`: ролі, виключення, ротація за `shift`, `NoAccountAvailable`.
- `ManagedAccount`: мапа `{kind}` → виняток; `GatewayDown` → `RateLimited(30)`.
- Споживачі: наявні тести `test_stages_infospace.py`, `test_adapter_telegram.py`,
  tgsearch, publish — підмінити `registry` на стаб, зберегти сценарії
  (бекоф, ротація, FloodWait, відвʼязка чатів).
- `test_no_direct_telethon.py`.

**Локальна інтеграція (дев-стек):**
1. `docker compose up tg-gateway`, у дев-БД лишити 3–5 реальних акаунтів з
   проксі (решту `is_active=False`). **Дев-БД — копія прод-акаунтів:** будь-який
   вхід у Telegram з локалі паралельно з продом ризикує AuthKeyDuplicated, тож
   спершу вибрати акаунти, яких прод не використовує. Профілактичний ремонт
   (`gateway_repair_proactive`) за замовчуванням вимкнено саме тому; на проді
   увімкнути після cutover.
2. `worker-info-collect` по 10 Telegram-джерелах, `tgs-stream` по 5 чатах,
   `publish` у тестовий канал.
3. Сценарії руками: зупинити gateway під час збору (споживачі → `RateLimited`,
   лічильники джерел не ростуть); зіпсувати проксі акаунта (перехід у
   `cooldown` → `repair` → або `ready`, або `needs_proxy`); два воркери на один
   акаунт одночасно (другий чекає lock, `AuthKeyDuplicated` не зʼявляється);
   SIGTERM gateway (всі `disconnect`, після старту клієнти піднімаються).
4. Критерій: 24 год на дев-стеку без `AuthKeyDuplicated`, без джерел у
   `down` через транспорт, `service_health` показує gateway.

## 8. Cutover на проді (вікно 20–30 хв)

0. Напередодні: `git push`, на сервері `git fetch` (без merge), `pg_dump` у `backups/`.
1. `docker compose … stop worker-info-collect worker-tgs-stream worker-tgs-screen
   worker-tgs-tag worker-publish worker-warm-up worker-spam-status worker-test-bot
   worker-proxy-health web mcp` — дочекатись, поки `service_ps` не покаже
   жодного, хто в Telegram.
2. `git merge --ff-only origin/main`; `docker compose build web` (нові
   залежності: aiohttp уже є); `manage.py migrate`.
3. `docker compose up -d tg-gateway`; перевірити `GET /health` (підключено 0,
   БД ok), логи без трейсбеків.
4. `docker compose up -d web mcp`; в адмінці «Перевірити живість» на 3 акаунтах
   → через gateway, стан `ready`, `gateway_connected=True`.
5. `docker compose up -d worker-publish worker-tgs-stream worker-info-collect …`
   по одному, дивитись логи 2–3 хв кожен.
6. `docker compose rm -sf worker-proxy-health`.
7. 30 хв спостереження: `service_health`, `sources_list problems_only`,
   `docker stats tg-gateway`.

**Відкат** (будь-який крок після 2): `git checkout <попередній sha>` на сервері,
`migrate accounts <попередня>` (нові поля адитивні, дані можна лишити —
відкат міграції не обовʼязковий), `docker compose up -d` старого складу,
`docker compose rm -sf tg-gateway`. Час: ~5 хв.

## 9. Ризики й що з ними

| ризик | мітигація |
|---|---|
| gateway — єдина точка відмови | `restart: unless-stopped`; споживачі при `GatewayDown` не псують стан; `service_health` кричить; healthcheck контейнера на `GET /health` |
| довгоживучі клієнти Telethon: дрейф, витік entity-кешу, тихі реконекти | idle-disconnect через Setting; `repair()` за `last_ok_at` > 4 год; метрика RSS у `/health`; перезапуск gateway раз на добу cron-ом як страховка на перший місяць |
| 117 одночасних підключень при старті | ліниве підключення при першому виклику + фонове прогрівання по 5 за раз |
| sync-воркер блокується на HTTP 120 с | таймаут на виклик; воркерів багато, це та сама ціна, що зараз |
| oператор змінив проксі в адмінці напряму (не через `replace_proxy`) | `save()` моделі при зміні `proxy_id` викликає `gateway.invalidate(id)`; gateway перечитує рядок перед реконектом |

## 10. Порядок робіт

1. Міграція полів + `state.py` + тести стейт-машини.
2. `_telethon.py` (перенесення `TelegramUserClient` як є) + `live.py` + `pool.py` + `server.py` + `/health`.
3. `managed.py` + `registry.py` + тести.
4. Переїзд споживачів у порядку: publish → infospace collect → tgsearch stream →
   сервісні воркери → admin/views → MCP → research. Після кожного — тести.
5. `repair.py`, видалення `proxy_health.py`, `account_exclusive*`, `run_async`.
6. `test_no_direct_telethon.py`, docker-compose, `service_health`, `AI-GUIDE.md`
   (розділ «Telegram-акаунти: тільки через `registry`/`ManagedAccount`»).
7. Локальна інтеграція §7 → cutover §8.

Вирішено (2026-09-19): **одна операція читання `scan`** замість `fetch_history`
і batch для стріму.

```
scan(chats=[{entity, min_id, limit, reverse}], patterns=[], media=None)
  -> [{chat_key, hits: [{mid, text, date, author_id, term, media}], max_id,
       n_seen, n_media, error}]
media = None | {"forward_to": peer, "per_tick": 5, "pause": 1.5, "which": "all"|"matched"}
```

- Один HTTP-виклик на акаунт під одним lock: gateway резолвить `forward_to`
  раз, читає чати послідовно від watermark, матчить регулярки (порожньо =
  повертати все), за бажанням пересилає фото/відео тим самим акаунтом
  (`which="all"` — усе до ліміту за тик, як стрім робить зараз; `"matched"` —
  лише збіги) і віддає метадані медіа.
- Gateway не знає про `MonitorChat`/`Source`/задачі: воркер мапить моделі на
  аргументи і результат назад. Константи конвеєра (`MEDIA_PER_TICK`, паузи)
  передаються в тілі, не живуть у gateway.
- infospace-збирач = `scan` з одним чатом, без патернів і медіа. Стрім tgsearch =
  `scan` з усіма чатами акаунта (~40 викликів на прохід замість 500).
- `fetch_history` у `ManagedAccount` лишається як зручна обгортка над `scan`
  для одного чату.
