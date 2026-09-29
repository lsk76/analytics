# Сервіс tgstat — доступ до TGStat з проду

Окремий stateless-сервіс у docker (`tgstat_service/`, сервіс compose `tgstat`),
через який решта системи користується TGStat: пошук каналів і чатів за словами
по каталогах, збір даних про канали, пошук публікацій, посилання на сторінки
tgstat.

**Стан на 2026-09-29: зроблено етап 1 — авторизація.** Пошук і збір додаються
наступними етапами (план унизу).

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

## HTTP API (внутрішня мережа compose, порт 8020)

| Метод | Шлях | Що робить |
|---|---|---|
| GET | `/health` | процес живий; `browser` — чи піднятий Chrome |
| GET | `/auth/status` | стан сесії за поточною сторінкою. `?reload=1` спершу перезаходить на головну: це оновлює кліренс, але збиває вхід, якщо він саме йде у VNC |
| POST | `/auth/login` | відкриває головну tgstat у вікні браузера, щоб увійти через VNC |
| GET | `/auth/screenshot` | PNG поточної вкладки: глянути, що там, без VNC |

`/auth/status` повертає `state`:

| state | значення | що робити |
|---|---|---|
| `ok` | залогінений, тариф Premium, CSRF є | нічого |
| `login_required` | сайт відкрився, входу немає | увійти (нижче) |
| `no_premium` | вхід є, тариф не Premium | пошук публікацій потребує Premium: увійти іншим акаунтом або оплатити |
| `cloudflare` | челендж або блок Cloudflare | відкрити VNC; зазвичай досить пройти челендж руками і, якщо треба, увійти |
| `error` | браузер не піднявся, таймаут, немає CSRF | `docker compose logs tgstat` |

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

Кожен наступний етап — ендпоінти в тому ж сервісі поверх того ж `Browser`
(запити йдуть `fetch` зі сторінки з CSRF, як у `tools/discovery/tgstat_parser/`),
потім MCP-інструменти в `backend/analysis/services/mcp_api/`, які ходять у
`http://tgstat:8020`, і рядок у `service_health`.

1. **Пошук каналів і чатів за словами по каталогах**: `/channels/search`,
   `/chats/search` (поля форми: `q`, `inAbout`, `participantsCountFrom`,
   `countries[]`, `categories`, `page`/`offset`), підбірки `/tag/<регіон>/items`.
2. **Збір інформації по каналу**: сторінка `/channel/@handle` і `/stat`
   (підписники, охоплення, ER, ІЦ, вік, категорія, гео).
3. **Пошук публікацій**: `/search` + `/search/list` (Premium), дати,
   `peerType`. Готовий парсер карток — `../sm-analytics/TelegramAnalytics/tgstat_html.py`.
4. **Посилання на tgstat**: кожен результат несе `tgstat_url`
   (`{base}/channel/@h`, `{base}/channel/@h/stat`, `{base}/channel/@h/<post_id>`).

Пошук серіалізується локом браузера (одна вкладка). Паралельність — не
вимога цього етапу.
