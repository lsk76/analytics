# Сервіс tgstat — доступ до TGStat з проду

Окремий stateless-сервіс у docker (`tgstat_service/`, сервіс compose `tgstat`),
через який решта системи користується TGStat: пошук каналів і чатів за словами
по каталогах, збір даних про канали, пошук публікацій, посилання на сторінки
tgstat.

**Стан на 2026-09-29:** авторизація на проді (акаунт Premium), HTTP-API пошуку
каналів, каталогів, картки каналу, пошуку публікацій і посилань. Далі —
MCP-інструменти в `backend/analysis/services/mcp_api/` і рядок у `service_health`.

## Чому окремий сервіс і чому браузер

У TGStat немає API для пошуку, яким ми могли б користуватись, тож працюємо
через сайт у справжньому Chrome (Playwright), як уже робили
`tools/discovery/tgstat_parser/` і `../sm-analytics` (`TelegramAnalytics/tgstat_search_client.py`).
Звідти один жорсткий факт:

> **Сесію tgstat прив'язано до профілю браузера, IP виходу і збірки браузера.**
> Змінилось будь-що з трьох — Cloudflare знову ставить челендж, і треба
> логінитись заново.

Звідси й будова сервісу:

- **Один постійний headed Chrome** на віртуальному дисплеї Xvfb у контейнері.
  Headless не підходить: він міняє User-Agent і ламає кліренс Cloudflare.
- **Вхід робить людина в ЦЬОМУ Ж браузері** через noVNC. IP, профіль і збірка
  збігаються з робочими автоматично, тож обходи на кшталт WireGuard, щоб
  «логінитись з IP сервера», які були в sm-analytics, не потрібні. Окремого
  логін-скрипта, який би бився із сервісом за блокування профілю, теж немає.
- **Stateless:** немає БД і немає стану між запитами. Рестарт переживає лише
  профіль Chrome (cookies = сесія) у томі `tgstat_profile`. Контейнер можна
  вбивати, перезбирати й піднімати знову без повторного входу, **якщо не
  змінилися збірка Chrome (тег образу Playwright) або IP**.
- Без Django: сервіс не ходить у БД і не тягне важкий образ web.
  Django-сторона звертатиметься до нього по HTTP (`http://tgstat:8020`) так
  само, як до `tg-gateway`.

## HTTP API даних (внутрішня мережа compose, порт 8020)

Усі GET, відповідь JSON. Кожен результат несе посилання: `tgstat_url`,
`tgstat_stat_url`, `tme_url` (у приватних `null`), у постах ще `tgstat_post_url`
і `tme_post_url`. **Кожна сторінка результатів = один запит до tgstat**, тому
`max_pages` задає прямо кількість запитів (див. «Темп і капча»).

| Шлях | Що | Параметри |
|---|---|---|
| `/channels/search` | пошук **каналів** за словами в назві (`in_about=1` — і в описі) | `q`, `in_about`, `min_subs`, `max_subs`, `country` (назва або id, дефолт «Россия», порожнє = будь-яка), `category`, `language` (назва або id, `app/filters.py`), `sort` (`participants`/`avg_reach`/`ci_index`/`members_7d`…), `limit` (100), `max_pages` (3) |
| `/catalog/tags` | список підбірок | `kind=geo` (регіональні) або `theme` |
| `/catalog/{tag}` | канали або **чати** підбірки, напр. `buratia-region` | `kind=channel\|chat`, `category_id`, `limit` (200), `max_pages` (5) |
| `/channel/{ref}` | картка каналу/чату: назва, гео/мова, категорія, РКН, опис, підписники (+добу/тиждень/місяць), ІЦ (+згадки/репости), середнє охоплення + ERR/ERR24, рекламне охоплення 12/24/48 год, вік і дата створення, публікації, ER | `ref` = `@h`, `h`, `t.me/h` або URL tgstat; `kind` |
| `/posts/search` | **пошук публікацій** (Premium) | `q`, `from`/`to` (`YYYY-MM-DD`), `peer_type` (`all`/`channel`/`chat`), `sort` (`date`/`views`), `hide_forwards`, `strong`, `extended`, `minus_words`, `limit` (100), `max_pages` (3) |
| `/links/{ref}` | посилання tgstat/t.me **без запиту** до tgstat | `post_id`, `kind` |
| `/raw` | сире тіло відповіді tgstat (діагностика парсерів) | GET `?path=/…`; POST `{"path", "form": [[k, v]…]}` |

Помилки: `400` — погані параметри, `404` — tgstat не знає каналу,
`503` + `state` (`captcha` / `login_required`) — потрібна людина у VNC,
`409` + `state=manual` — саме йде ручний вхід.

Чого в tgstat **немає**: пошуку чатів за словами (`/chats/search` дає 500,
`/chats` — 404). Чати дістаються лише з підбірок: `/catalog/{tag}?kind=chat`.
Пошук публікацій охоплює і повідомлення в чатах (`peer_type=chat`).

Код: `tgstat_service/app/ops.py` (операції), `app/parse.py` (парсери, тести на
знятих сторінках у `tests/fixtures/`). Взято й перевірено: поля форми пошуку
каналів і каталогів — з `tools/discovery/tgstat_parser/`, схема пошуку
публікацій (`/search`, далі `/search/list` з page/offset зі сторінки) — з
`../sm-analytics`.

### Темп і капча

tgstat має власний антибот: 2026-09-29 уже за ~10 запитів із паузою 1.5 с він
відповів **429 «Подозрение на робота» з reCAPTCHA** (AJAX: `{"status":"restricted"}`).
Тому:

- запити йдуть по одному, пауза `TGSTAT_REQUEST_DELAY` (дефолт 4 с) + до 50%
  випадково; сторінки (`GET`) відкриваються навігацією вкладки, як у людини;
- капча розпізнається (`state=captcha` у `/auth/status` і 503 в API), а не
  парситься як «0 результатів»;
- **капчу проходить людина**: `POST /auth/manual` → VNC → на tgstat пройти
  reCAPTCHA → закрити вкладку. Автоматично її не обходимо.

## MCP: tgstat у двох місцях

Інструменти `tgstat_*` є В ОБОХ серверах, і це не дубль логіки: уся робота з
сайтом однаково в сервісі (`http://tgstat:8020`), різниця лише в тому, хто
викликає.

- **`tg-analytics` (основний шлях)** — `backend/analysis/services/mcp_api/tgstat.py`.
  Ті самі вісім інструментів, але всередині звичайного MCP-шару: право Django
  з `mcp_api/perms.py` (читання — `analysis.view_channel`, пошук публікацій —
  `analysis.view_post`, ручний вхід — `analysis.change_setting` + `mcp:admin`),
  слід в аудиті, видимість така сама, як в адмінці. Нічого окремо реєструвати
  не треба: хто бачить `tg-analytics`, той бачить і tgstat. Адреса сервісу —
  `TGSTAT_API_URL` (дефолт `http://tgstat:8020`), тайм-аут читання
  `TGSTAT_API_TIMEOUT` (600 с); на з'єднання — завжди 5 с, щоб виклик із
  мережі, де tgstat не видно, падав одразу, а не через десять хвилин.
  **У dev-стеку сервісу tgstat немає** (він у профілі `tgstat` на проді, і
  dev-мережа до прод-контейнера не маршрутизується) — там інструменти
  відповідають «сервіс tgstat не відповідає», і це нормально.
- **`tgstat` (окремий stdio)** — запасний шлях для того, хто має ssh на прод і
  не хоче підіймати Django-шар. Прив'язаний до образу контейнера: якщо
  контейнер старіший за додавання `app/mcp_server.py`, `python -m app.mcp_server`
  падає з `No module named app.mcp_server` — лікується пересозданням
  контейнера з поточного образу.

Власний MCP-сервер, НЕ частина `tg-analytics`: `tgstat_service/app/mcp_server.py`,
stdio, живе в контейнері `tgstat` і ходить у HTTP-API вище на `127.0.0.1:8020`
(браузер і темп запитів — один на всіх). Зареєстрований у `.mcp.json` як
`tgstat`; обгортка `tgstat_service/mcp-stdio.sh` робить

```bash
ssh tg-analytics 'cd /opt/tg-event-analytics && docker compose -f docker-compose.yml \
  -f docker-compose.monitor.yml exec -T tgstat python -m app.mcp_server'
```

(`TGSTAT_SSH=local` — без ssh, для Claude Code на самому сервері; `.mcp.json`
бере його з `TGA_PROD_SSH`).

| Інструмент | Що |
|---|---|
| `tgstat_status` | стан сесії, тариф, капча/Cloudflare, що робити |
| `tgstat_channels_search` | пошук каналів за словами + фільтри |
| `tgstat_catalog_tags` | список підбірок (geo / theme) |
| `tgstat_catalog` | канали або чати підбірки |
| `tgstat_channel` | картка каналу/чату зі статистикою |
| `tgstat_posts_search` | пошук публікацій (Premium) |
| `tgstat_links` | посилання tgstat/t.me без запиту до tgstat |
| `tgstat_manual_login` / `tgstat_manual_finish` | ЗМІНЮЄ СТАН: ручний вхід/капча у VNC |

Капча/розлогін повертаються як «⚠ …» з інструкцією для людини, а не як
порожній результат.

Тести трьома шарами, кожен про своє:

| що | де | як запустити |
|---|---|---|
| розмітка tgstat → структури | `tgstat_service/tests/test_ops.py`, `test_parse.py` | у контейнері сервісу |
| **ендпоінти HTTP-API** (назви й дефолти query-параметрів, 503 `captcha`/`login_required`, 409 `manual`, 400/404, `/links` без запиту) | `tgstat_service/tests/test_api.py` — справжній aiohttp-застосунок, підставлений лише браузер | у контейнері сервісу |
| MCP-інструменти | `tgstat_service/tests/test_mcp.py` (окремий сервер), `backend/analysis/tests/test_mcp_tgstat.py` (шар `tg-analytics`) | відповідно в контейнері сервісу / `web` |

У образі сервісу pytest не стоїть (він не потрібен у бою), тож локально —
одноразовим контейнером, без дотику до робочого:

```bash
docker run --rm -v "$PWD/tgstat_service:/svc" -w /svc --entrypoint sh \
  tg-event-analytics-tgstat:latest -c 'pip install -q pytest && python -m pytest tests -q'
```

Інструменти в `tg-analytics` тестуються моками (у контейнері `web` немає ні
сервісу, ні Playwright), але **відповіді не вигадані**: знімки справжніх
маршрутів лежать у `backend/analysis/tests/fixtures/tgstat_api/`, генерує їх
`tgstat_service/tests/dump_api_fixtures.py` (команда — у його докстрінгу).
Змінився формат API — перегенеруй, і розбіжність покажуть тести, а не бій.

Стани сервісу однакові для обох: `captcha` і `login_required` приходять як 503,
`manual` (іде ручний вхід) — як 409, і кожен перекладається в пораду людині, а
не в сирий трейсбек.

Доступу через мережевий MCP (OAuth, `mcpauth`) у цього сервера **немає** —
лише stdio по ssh, тобто тим, хто має ssh на прод.

## HTTP API сесії

| Метод | Шлях | Що робить |
|---|---|---|
| GET | `/health` | процес живий; `browser` — чи піднятий Chrome |
| GET | `/auth/status` | стан сесії за поточною сторінкою. `?reload=1` спершу перезаходить на головну: це оновлює кліренс, але збиває вхід, якщо він саме йде у VNC |
| POST | `/auth/login` | відкриває головну tgstat у вікні браузера, щоб увійти через VNC |
| GET | `/auth/screenshot` | PNG поточної вкладки: глянути, що там, без VNC |
| POST | `/auth/manual` | **ручний вхід у звичайному Chrome**: сервіс закриває свій Chrome під Playwright і запускає на тому ж профілі той самий бінарник Chrome як звичайну програму, без Playwright/CDP і прапорців автоматизації |
| POST | `/auth/manual/finish` | закрити звичайний Chrome (SIGTERM, cookies зберігаються) і повернути Chrome під Playwright; відповідає станом сесії |

`/auth/status` повертає `state`:

| state | значення | що робити |
|---|---|---|
| `ok` | залогінений, тариф Premium, CSRF є | нічого |
| `login_required` | сайт відкрився, входу немає | увійти (нижче) |
| `no_premium` | вхід є, тариф не Premium | пошук публікацій потребує Premium: увійти іншим акаунтом або оплатити |
| `captcha` | власний антибот tgstat (429 + reCAPTCHA) за частоту запитів | ручний режим (`/auth/manual`), пройти капчу у VNC, закрити вкладку |
| `cloudflare` | челендж або блок Cloudflare | відкрити VNC; зазвичай досить пройти челендж руками і, якщо треба, увійти |
| `error` | браузер не піднявся, таймаут, немає CSRF | `docker compose logs tgstat` |
| `manual` | іде ручний вхід у звичайному Chrome | увійти у VNC, потім закрити вкладку або `POST /auth/manual/finish` |

Ще в тілі відповіді: `user`, `plan`, `url`, `title`, `detail`, `checked_at`,
`usable`, а коли сесія непридатна, то й `how_to_login`.

Маркери стану взято з живої розмітки tgstat.ru: `[data-logout-button]` /
`#topbar-userdrop` є лише в залогіненого, `.account-user-name` містить ім'я,
`.account-position` тариф, `meta[name=csrf-token]` потрібен для AJAX-пошуку.
Код: `tgstat_service/app/session.py`, тести `tgstat_service/tests/`.

**Keepalive.** Раз на `TGSTAT_KEEPALIVE` секунд (дефолт 1800) сервіс
перевіряє сесію. Живу сесію перевіряє з перезаходом на сайт, бо кліренс
Cloudflare протухає, коли профіль простоює. Неживу лише спостерігає, щоб не
збити вхід, який саме йде. Зміна стану потрапляє в лог (`WARNING сесія tgstat: ok -> …`).

## Як авторизуватись на продакшені

Прод: `ssh tg-analytics`, каталог `/opt/tg-event-analytics`
(див. `deploy/README.md`).

### 0. Один раз: секрети і профіль compose

У `/opt/tg-event-analytics/.env`:

```bash
COMPOSE_PROFILES=tgstat          # без цього compose сервіс tgstat не піднімає
TGSTAT_VNC_PASSWORD=<довгий випадковий>   # без пароля VNC не стартує
# необов'язкові:
# TGSTAT_BASE_URL=https://tgstat.ru      # або дзеркало https://uk.tgstat.com
# TGSTAT_PROXY=http://user:pass@host:port   # сесія прив'яжеться до IP проксі
# TGSTAT_CHANNEL=chrome                  # "" — вбудований Chromium
```

Пароль VNC можна згенерувати так: `openssl rand -base64 18`. У dev-стенді
(`/opt/tg-event-analytics-dev`) `COMPOSE_PROFILES` не ставимо: другий браузер
з тим самим акаунтом там не потрібен.

### 1. Підняти сервіс

```bash
cd /opt/tg-event-analytics
docker compose -f docker-compose.yml -f docker-compose.monitor.yml up -d --build tgstat
docker compose -f docker-compose.yml -f docker-compose.monitor.yml ps tgstat   # healthy
```

### 2. Відкрити екран браузера (з локальної машини)

noVNC опубліковано лише на `127.0.0.1:6080` сервера, тому до нього дістаємось
SSH-тунелем:

```bash
ssh -N -L 6080:127.0.0.1:6080 tg-analytics
```

Далі в браузері на своїй машині відкрити http://localhost:6080/vnc.html →
Connect → пароль `TGSTAT_VNC_PASSWORD`. Видно вікно Chrome сервісу.

### 3. Увійти

Якщо у вікні не tgstat, виведіть його на передній план:

```bash
docker compose -f docker-compose.yml -f docker-compose.monitor.yml exec tgstat \
  python -c "import urllib.request as u; print(u.urlopen(u.Request('http://127.0.0.1:8020/auth/login', method='POST')).read().decode())"
```

У вікні VNC пройти Cloudflare, якщо він з'явився, і увійти на tgstat
акаунтом із Premium («Войти» → Telegram). Нічого не закривати: це той самий
браузер, яким сервіс працюватиме далі.

**Якщо Cloudflare або вхід tgstat не пускають браузер під Playwright**, увійдіть
у звичайному Chrome. Це той самий бінарник і той самий профіль, тож TLS-відбиток
не зміниться:

```bash
docker compose -f docker-compose.yml -f docker-compose.monitor.yml exec tgstat \
  python -c "import urllib.request as u; print(u.urlopen(u.Request('http://127.0.0.1:8020/auth/manual', method='POST')).read().decode())"
```

У VNC з'явиться звичайний Chrome на tgstat.ru. Увійдіть, потім закрийте вкладку.
Коли остання вкладка закрита, Chrome завершується, і сервіс сам повертає свій
браузер (або `POST /auth/manual/finish`). Поки йде ручний вхід, `/auth/status`
відповідає `manual`, а keepalive браузер не чіпає.

### 4. Перевірити

```bash
docker compose -f docker-compose.yml -f docker-compose.monitor.yml exec tgstat \
  python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8020/auth/status?reload=1').read().decode())"
```

Очікується `"state": "ok"`, `"plan": "Premium"`. Після цього тунель можна
закрити, бо сесія лежить у томі `tgstat_profile`.

### Коли сесія відвалилась

1. `GET /auth/status?reload=1`: подивитись `state` і `detail`.
2. `GET /auth/screenshot` (або VNC): що саме на екрані.
3. `cloudflare` / `login_required`: кроки 2–4 вище.
4. Типові причини: оновили тег образу Playwright (нова збірка Chrome), змінився
   IP сервера або проксі, tgstat сам розлогінив (вхід з іншого місця), скінчився
   Premium.

## Чого не робити

- **Не публікувати 6080 назовні** і не ставити його за nginx без авторизації:
  хто бачить VNC, той керує залогіненим акаунтом tgstat.
- **Не піднімати другий екземпляр** на тому ж профілі: Chrome тримає лок профілю.
- **Не міняти тег базового образу** (`mcr.microsoft.com/playwright/python:v1.49.0-jammy`)
  і `playwright==` в `requirements.txt` поодинці: вони мусять збігатися. Будь-яка
  зміна = нова збірка Chrome = перелогін.
- **Не вмикати headless.**

## Наступні етапи (план)

1. ~~MCP-інструменти `tgstat_*` у `backend/analysis/services/mcp_api/`~~ —
   зроблено, див. розділ «MCP: tgstat у двох місцях».
2. Рядок tgstat у `service_health` (стан сесії, капча).
3. Імпорт знайдених каналів/чатів у довідник `Channel` (з `region_subject`).
