# Моніторинг інформаційного простору (infospace) — дизайн

> **Статус: Phase 0-4 РЕАЛІЗОВАНО (крім VK).** Дата: 2026-07-09. Три адаптери
> наживо: RSS (`gazeta-n1.ru`→5 подій), Web (`baikal-daily.ru`), Telegram
> (`ulan_smi`). Phase 4: самоперевірка скраперів (канарки+`info_healthcheck`+
> health-бейдж 🟡+золоті фікстури) + авто-аудит подій (тумблер review_enabled).
> 95 тестів зелені. Phase 0-3 у `main`. VK свідомо пропущено. Дизайн нижче.
> Рішення замовника: свіжість **5–15 хв**; опис події **оновлюваний**;
> вікно збігу **ковзні 24 год**; **кілька тематичних задач** над спільним
> пулом джерел; **100+ джерел**, RSS-first (§14); події одразу **approved**;
> **теги в Phase 1** (з адмінки); ретеншн сирих постів **2 доби** (§13, §14).

## 1. Ідея за один абзац

Четвертий конвеєр (`pipeline="infospace"`) поруч із `events`, `monitor`,
`research`. Безперервно опитує **джерела** (Telegram через акаунти, RSS, сайти
зі скрапінгом; згодом VK) → складає сирі `Post` → **AI-фільтр** релевантності →
**зіставлення з подіями останніх 24 год** (той самий факт → приєднати пост,
оновити опис; інший факт → нова подія) → `Event`. Аналітика (графіки, матриця,
`metrics.py`) працює без змін, бо результат — та сама модель `Event`.

## 2. Що реюзаємо, що нове

Головна теза: **новий код неминучий лише для збору джерел і планувальника**.
Фільтр і зіставлення подій — це вже наявні патерни інших конвеєрів.

| Крок | Рішення | Донор |
|---|---|---|
| Скелет воркер/стадія/claim-черга | реюз | усі конвеєри (`_claim_posts`, `_advance`) |
| Моделі `Post`, `Event` | реюз (+ дрібні поля) | усі |
| **Збір джерел** (RSS/сайти/TG-акаунт) | **новий** | — (TeleZip це не вміє) |
| Фільтр релевантності (дешева LLM «так/ні») | реюз патерну прескріну | `monitor` (`mon_prescreen`) |
| **Зіставлення «є подія / нова»** | реюз дедуп-логіки | `events` (`dedup_once`, `_create_event`, `_attach_posts`) |
| **Планувальник** (безперервний полінг) | **новий** | — (інші — період-джоби) |
| Форма задачі, інлайни, health-дашборд | реюз конвенцій адмінки | `monitor`/`research` |

**Чому не «просто перелаштувати monitor»:** його збір — TeleZip (не вміє
RSS/сайти; Telegram нам треба через акаунти), а стадія подій навмисно робить
**1 коментар → 1 подія без дедупу** — протилежність до «є подія → приєднати».
Тому донор зіставлення — `events`, а не `monitor`.

## 3. Наслідування конвенцій наявних конвеєрів

Усі три роблять однаково — робимо так само:

1. **Роутинг воркера по префіксу стадії** (`run_worker._tasks`): додаємо гілку
   `stage.startswith("info_")` → `pipeline="infospace"`.
2. **Реєстр ранерів** `ALL_RUNNERS`: `{stage → runner_once}`.
3. **Форма задачі — картка етапів на конвеєр:** `get_fieldsets()` світч по
   `pipeline` → додаємо `_FS_INFOSPACE`; четвертий запис у масив `CARDS`
   (`analysistask/change_form.html`); JS ховає чужі етапи й переставляє інлайн
   джерел за маркер-класом.
4. **Список джерел = інлайн на задачі:** `SourceSubscriptionInline` — точне
   дзеркало `MonitorChatInline` (`MonitorChat ↔ Channel` → `SourceSubscription
   ↔ Source`).
5. **Промпти в полях задачі**, порожнє = дефолт із коду.

**Єдиний свідомий відхід:** збір не через `ResearchRun/CollectChunk/TeleZip`
(період-джоби), а безперервний полінг `Source`. Тому стадія `info_collect`
працює **по джерелах, а не по задачах**, і замість дашборда «Збори» —
health-дашборд джерел. Усе *після* збору лишається канонічним.

## 4. Модель даних

### Нове

**`Source`** — джерело в глобальному довіднику:
- `kind` (`telegram`/`rss`/`web`/`vk`), `name`, `url` (канонічний ідентифікатор),
  `region_subject` (FK), `language`.
- web: `scraper_key` (порожньо = автоекстракція), `config` (JSON: селектори,
  ліміти, headers).
- telegram: `tg_account` (FK `accounts.TelegramAccount`; null = пул).
- розклад/health (пише лише `info_collect`): `is_active`, `poll_interval_sec`,
  `next_poll_at`, `locked_at`, `state` (watermark адаптера), `last_ok_at`,
  `last_error`, `consecutive_failures`.
- `unique(kind, url)`.

**`SourceSubscription`** — підписка задачі на джерело (аналог `MonitorChat`):
`task`, `source`, `is_active`, `priority`, `notes`; `unique(task, source)`.
Пост створюється окремо на кожну підписану задачу — інваріант `unique(task,
url)` не порушується, у кожної теми свій фільтр і свої події.

### Адитивні зміни

- `Post`: `source` (FK, null), `title`; нові стадії `info_collected`,
  `info_screened` (влазять у `max_length=16`). Решта полів — як є;
  `region_subject` копіюємо з `source` на вставці (тримаємо денормалізацію).
- `Event`: `last_post_at` (DateTimeField, index) — час останнього приєднаного
  поста; заповнює лише `info_event`. Потрібно для вікна 24 год без `MAX()` по
  мультимільйонній таблиці й для сортування «живі сюжети зверху».
- `AnalysisTask`: значення `PIPELINE_INFOSPACE`; поля `info_screen_model`,
  `info_screen_prompt`, `info_judge_prompt`, `info_match_window_hours` (24),
  `info_update_summaries` (bool), `info_retention_days` (int, дефолт 2 — §6.1),
  `tag_categories` (M2M, вже є) + `info_tagger_prompt` (§4.1).

**Лічильники infospace-події:** `channel_count` = унікальні джерела
(кросджерельна підтвердженість — головна метрика довіри), `reach` = сума
підписників лише TG-каналів (у звітах дивитись count/джерела, не reach).

### 4.1 Теги (у Phase 1, налаштовні з адмінки)

Подія позначається тегами (тема/рубрика; «гучна подія» — теж окремий тег).
Реюзаємо наявний механізм: `task.tag_categories` (M2M на `TagCategory`) +
промпт `info_tagger_prompt` — точно як monitor кладе теги за `tagger_prompt`.
Теги проставляє **скрін** (він і так читає текст — див. §6, `info_screen`
повертає ще й `tags` за схемою обраних категорій), подія успадковує їх при
створенні / доповнює при attach.

Правила проєкту (обов'язкові): нова категорія → **створити рядок `TagCategory`**
(інакше `?e=1` в адмін-фасеті); закриті категорії канонізуються лише з
сід-списку. Промпт і набір категорій — повністю з адмінки (порожньо = дефолт
із `infospace/prompts.py`).

## 5. Спільний інтерфейс джерела

Пакет `analysis/services/infospace/`: `adapters/` (base + telegram/rss/web),
`scrapers.py` (реєстр кастомних), `stages.py`, `prompts.py`.

**Контракт** (закріплений параметризованим тестом по реєстру `ADAPTERS`):

```python
@dataclass
class RawItem:
    external_id: str; url: str; title: str; text: str
    posted_at: datetime | None; author: str; meta: dict

class BaseSourceAdapter:
    kind: str
    def fetch(self, source) -> list[RawItem]:
        """Лише НОВІ елементи відносно source.state. НЕ пише в БД
        (персистенцію робить стадія). Мутує state у пам'яті."""
```

Правила: `fetch` без запису в БД (тестується без Django); ідемпотентність
(+ друга лінія — `unique(task, url)`); помилки → виняток (backoff рахує стадія);
ліміт `config.max_items` (перший полінг — окремий `backfill_limit`); спільна
`canonical_url()` зрізає `utm_*`/`#fragment` (rss- і web-версія лінка = один Post).

**Адаптери:**
- **telegram** — `iter_messages(entity, min_id=state.last_msg_id)` акаунтом;
  розширюємо наявний `TelegramUserClient` методом `fetch_history`; публічні
  канали без вступу; `FloodWaitError` → відсунути `next_poll_at`, без інкременту
  failures.
- **rss** — `feedparser` + умовний GET (etag/modified → 304 задарма);
  `config.full_text` дотягує повний текст тим самим екстрактором, що web.
- **web** — discovery (лінки статей по CSS-селектору/regex, у межах домену) +
  extraction (дефолт **trafilatura**; override `config.selectors`; крайній
  випадок — кастомний клас у `SCRAPERS`). JS-rendered/анти-бот — поза Phase 1–2.
- **vk** — зарезервовано (Phase 4).

## 6. Стадії конвеєра

### `info_collect` — полінг джерел (по джерелах, не по задачах)

Єдина «taskless»-стадія (одне джерело живить кілька тем — полінг один).
`run_worker` дістає множину `TASKLESS_STAGES = {"info_collect"}`, для якої
цикл `for task` замінюється на `while runner()`.

Прохід: claim джерела за `next_poll_at` (`skip_locked`) → `fetch` →
фан-аут `update_or_create(Post)` по активних підписках (`stage=info_collected`,
не відкочувати наявний пост назад) → успіх: зберегти `state`, `next_poll_at =
now + interval ± jitter`, `failures=0`; збій: `next_poll_at = now + interval *
2^failures` (cap 6 год). **1 репліка** (Telethon-сесія не терпить паралельних
конекшенів — та сама грабля, що з TeleZip до слотів).

### `info_screen` — AI-фільтр (реюз патерну прескріну monitor)

`_claim_posts(task, "info_collected")` → на пост один виклик дешевої моделі
(`task.info_screen_prompt`), строгий JSON: `{relevant, signature, summary,
region, tags{<категорія>: [...]}}` (набір категорій у `tags` будується з
`task.tag_categories` — як `build_classify_prompt` в events). Скрін+екстракція+
тегування в **одному** виклику (не платимо тричі). Нерелевантні → одразу `done`;
релевантні → `info_screened` з `classification`. Регіон (якщо `geo_enabled`) →
`resolve_region()`, теги → `normalize.resolve_in_category()`, інакше регіон
джерела. Битий JSON → `attempts`, після 3 → `failed`.

### `info_event` — зіставлення (реюз дедуп-логіки events)

Серце флоу. Обробляє по одному посту під `pg_advisory_xact_lock(task_id)` (два
воркери не створять дубль-подію). Переюзаємо `_create_event`/`_attach_posts` зі
`stages.py`; нове — лише відбір кандидатів по ковзному вікну й легший суддя:

1. Кандидати: `Event(task, last_post_at ∈ [post.posted_at ± W])`,
   `W = info_match_window_hours`; свій регіон — пріоритет. Вікно рахується від
   **дати поста**, не `now` (бекфіл/відставання матчаться коректно).
2. Скоринг `token_set_ratio(signature, summary)`, top-K судді; нема кандидатів →
   `_create_event` без LLM.
3. Суддя (`info_judge_prompt`): `{verdict: attach|new, event_id, update_summary,
   new_summary}`. Правило: уточнення/деталі/цифри = той самий факт; інша
   дія/місце/учасники = нова подія.
4. **attach** → `_attach_posts` (перерахунок post_count / джерела / reach /
   `last_post_at`; теги поста додаються до тегів події); якщо `update_summary`
   і `info_update_summaries` — переписати `summary` (консолідований, ≤3 речення;
   `event_date` НЕ чіпаємо).
   **new** → `_create_event` з тегами поста, `review_status=approved`.

**Аудит:** подія одразу `approved` (як monitor — контур скрін+суддя вже
відфільтрував; вимога свіжості 5–15 хв не дає тримати `pending`). Якщо колись
поповзуть хибнопозитиви — вмикається наявний авто-review (`review_enabled`) без
переробок.

Наслідок ковзного вікна: сюжет «живе», поки пости прилітають частіше ніж раз на
24 год; затих на добу+ → наступний пост відкриває нову подію (усвідомлений
компроміс, обрано замовником).

### 6.1 Ретеншн сирих постів

Нерелевантні `done`-пости чистяться — тримаємо лише останні
`task.info_retention_days` діб (дефолт 2, редагується в адмінці). Легка стадія/
cron `info_retention` раз на добу видаляє `Post(task, stage=done,
is_relevant≠True, posted_at < now − N діб)`. **Не чіпає** пости, приєднані до
події (`event_id` не NULL) — вони частина даних. Це прибирає розростання
таблиці від 100+ джерел × полінг кожні 10 хв.

## 7. Воркери та адмінка

**compose** (3 сервіси, реюз web-образу):
```yaml
worker-info-collect:   … --stage info_collect     # 1 репліка!
worker-info-screen:    … --stage info_screen      # масштабується
worker-info-event:     … --stage info_event       # advisory lock
worker-info-retention: … --stage info_retention   # чистка сирих постів, раз/добу
```

**Адмінка:**
- **Source changelist** — health-бейдж (🟢/🟡/🔴 + last_error), `last_ok_at`,
  `next_poll_at`, постів/24год; фільтри kind/health/регіон/задача; дії «Опитати
  зараз», «Тестовий збір (dry-run)» (показати перші N RawItem без запису),
  актив/деактив.
- **`SourceSubscriptionInline`** на задачі й на Source.
- **Форма задачі** — картка `_FS_INFOSPACE` + запис у `CARDS` + маркер-клас
  (див. §3).
- **Event** — без змін; корисне сортування по `last_post_at`.
- infospace **не** використовує дашборд «Збори» (безперервний) — стан дивимось
  на health-дашборді Source.

## 8. Самоперевірка джерел і скраперів (Phase 4)

Скрапери й селектори ламаються **тихо**: сайт переверстали → discovery віддає
0 лінків або extraction — сміття/порожнечу, а конвеєр «успішно» нічого не
збирає. Health-поля з §4 (`consecutive_failures`, `last_ok_at`) ловлять лише
*винятки*, не «успіх без користі». Тому окремим кроком — активна перевірка:

- **Пасивні сигнали одразу (Phase 1–2), без нового коду:**
  - «постів/24год» у списку Source: джерело, що місяцями лило й раптом дало 0 —
    підозра на злам;
  - ручний **dry-run** (§7) — виконати `fetch` без запису, очима звірити
    перші RawItem після редизайну сайту.

- **Активна самоперевірка (Phase 4) — РЕАЛІЗОВАНО** (`stages.evaluate_quality` +
  `info_healthcheck_once`; health-поля `Source.quality_ok/quality_note/
  last_healthcheck_at`; бейдж 🟡 + «пости/24г» в адмінці; дія «Самоперевірка
  зараз»; kind-залежні канарки — web вимагає тіло статті, rss лише непорожній
  контент). Оригінальний план:
  1. **Канарки якості на `fetch`** — правила «схоже на робоче»: discovery дав
     ≥1 лінк; у extraction непорожні `title` і `text` довший за поріг; є
     `posted_at`. Порушення → джерело `🟡 підозра` (не `🔴`, бо це не виняток),
     `last_error="extraction: порожнє тіло на N/K сторінок"`.
  2. **Періодичний воркер `info_healthcheck`** (окрема легка стадія або cron):
     раз на добу ганяє dry-run по кожному web-джерелу, застосовує канарки, пише
     health. Показує деградацію ще до того, як «постів/24год» впаде в нуль.
  3. **Золоті фікстури на регресії** — для кастомних скраперів (`SCRAPERS`)
     зберігати збережений HTML + очікуваний RawItem; тест проганяє скрапер по
     фікстурі. Ловить поломку скрапера у нашому коді (не на боці сайту).
  4. **Дрейф-детектор (опційно)** — різкий обвал середньої довжини тексту чи
     частки статей із датою по джерелу за вікно → алерт (пов'язано з §12 п.6
     про сповіщення).

Мета — щоб «зламаний скрапер» був **видимим станом джерела**, а не мовчазною
дірою в даних.

## 9. Тести (заразом закладаємо тест-інфраструктуру проєкту)

Тестів зараз немає — цей флоу заходить із нею. Стек: `pytest`, `pytest-django`,
`factory-boy`, `respx` (мок http), `freezegun` (час). Конфіг `pytest.ini` +
`conftest.py`, тести в `analysis/tests/`, запуск `docker compose exec web pytest`.
LLM і Telethon у тестах — фейки через DI.

Покриття: `canonical_url`; кожен адаптер (rss: etag/304, дати, guid-дедуп;
web: discovery по селектору/regex, чужі домени, extraction, 404; telegram:
watermark, backfill, FloodWait); контракт адаптерів (параметризовано); стадії
(`info_collect` claim/фан-аут/backoff; `info_screen` relevant/irrelevant/битий
JSON; `info_event` new-без-кандидатів / attach+лічильники / attach+update /
межі вікна / серіалізація «два пости → 1 подія»); міграції не ламають старі
конвеєри.

## 10. Нові залежності

`feedparser`, `trafilatura`, `selectolax`, `httpx` (+ dev: `pytest`,
`pytest-django`, `factory-boy`, `respx`, `freezegun`). Нових env — не треба
(TG api_id/hash і OpenRouter-ключі вже є).

## 11. Фази

| Фаза | Обсяг |
|---|---|
| **0. Скелет** | бекап → міграції (Source, SourceSubscription, поля Post/Event/Task); реєстри адаптерів; run_worker-роутинг + `TASKLESS_STAGES`; тест-інфра (перший тест зелений) |
| **1. RSS end-to-end** | rss-адаптер + 3 стадії (info_event на `_create_event`/`_attach_posts`) + теги (§4.1) + ретеншн (§6.1) + адмінка Source + `import_sources` + промпти + тести → стрічка→подія в адмінці |
| **2. Web** | web-адаптер (discovery + trafilatura + селектори + кастомні), перевірка RSS доменів §14, dzen-рішення, dry-run, тести |
| **3. Telegram** | `fetch_history`, tg-адаптер, пул акаунтів, FloodWait, Channel-лінк, тести (тест-канал `t.me/ulan_smi/28921`) |
| **4. Розширення** | самоперевірка скраперів (§8: канарки, `info_healthcheck`, золоті фікстури); VK; авто-аудит (реюз review-воркера); push-сповіщення |

Кожна фаза мержиться окремо й не чіпає events/monitor/research.

## 12. Ризики

- **TG-акаунти — головний.** Полінг = FloodWait/бан: інтервали ≥10 хв, jitter,
  публічні без join, **окремі акаунти, не особисті**. 1 репліка info-collect
  (одна StringSession з двох процесів = конфлікт).
- **Скрапери ламаються тихо** (редизайн сайту) → пасивно health-бейджі +
  «постів/24год» + dry-run; активна самоперевірка — §8 (Phase 4).
- **Вартість LLM** = 1 скрін × пост × підписану задачу → дешева модель + ліміти.
- **Ковзне вікно склеює довгі сюжети** — усвідомлено; перемикач режиму на задачу
  за потреби (Phase 4).
- Перед міграціями — **бекап** `pg_dump` (правило проєкту).

## 13. Рішення (відповіді замовника, 2026-07-09)

1. **Джерела** — перелік є (§14). Стратегія: **де є робочий RSS — беремо RSS**,
   решта — web-скрапер. TG-тест: `t.me/ulan_smi/28921`. Сідер-команда
   `import_sources` (CSV/JSON) + перевірка RSS кожного домену на Phase 1–2.
2. **Обсяг — 100+ джерел** → скрін дешевою моделлю обов'язковий; `info_screen`
   масштабується репліками; `info_collect` — 1 репліка (див. ризики).
3. **Аудит — approved одразу** (§6): контур скрін+суддя достатній, свіжість
   важливіша; авто-review вмикається полем за потреби.
4. **Ретеншн — чистимо, тримаємо 2 доби** (§6.1), поле `info_retention_days`
   в адмінці.
5. **VK — пропускаємо**, додамо пізніше (Phase 4, `kind="vk"` зарезервовано).
6. **Теги — у Phase 1** (§4.1): подія позначається тегами (тема; «гучна подія» —
   окремий тег), набір категорій і промпт `info_tagger_prompt` — з адмінки.
   Push-сповіщення поки не робимо (тег = маркер у самій події).

## 14. Інвентар джерел на старті (для `import_sources`)

Перед кодуванням Phase 1 — для кожного домену перевірити наявність RSS
(`/rss`, `/feed`, `<link rel=alternate type=application/rss+xml>` у `<head>`);
є → `kind=rss`, нема → `kind=web` (discovery+extraction). Прив'язати кожен до
`region_subject`. ~25 доменів по 8 республіках:

| Домен | Регіон | Примітка |
|---|---|---|
| `gazeta-n1.ru`, `baikal-daily.ru`, `infpol.ru` | Бурятія | регіональні новинні, ймовірно RSS |
| `gazetarb.ru` | Башкортостан | |
| `sakhaday.ru`, `yk24.ru`, `yakutia.mk.ru`, `yakutiamedia.ru` | Саха (Якутія) | |
| `inkazan.ru`, `realnoevremya.ru`/`.com` | Татарстан | `.com` — англ. версія |
| `tyva-news.ru`, `tuvaonline.ru` | Тива | |
| `malgobek.bezformata.com` | Інгушетія | bezformata — агрегатор регіону |
| `vtinform.com`, `tmgnews.ru`, `vz.ru` | (уточнити регіон) | |
| `novayagazeta.eu`, `meduza.io`, `currenttime.tv`, `news-pravda.com` | незалежні/крос-регіон | тема відфільтрує гео |
| `osw.waw.pl` | аналітика (англ.) | think-tank, рідкісний потік |
| **`dzen.ru`** | агрегатор | **окремий випадок**: JS-важкий, `/a/` статті + `/news/story` агрегації — кастомний скрапер у `SCRAPERS` або пропустити (дублює першоджерела) |

**Граблі інвентарю:**
- `dzen.ru` — не звичайний сайт (агрегатор Яндекса, JS-рендер); або кастомний
  скрапер, або **виключити** (його матеріали приходять і з першоджерел напряму).
- `bezformata` — сам агрегує регіональні прес-релізи; canonical_url має вести на
  оригінал, щоб не дублювати з першоджерелом.
- Дзеркала доменів (`realnoevremya.ru`/`.com`) — різні `Source`, але
  `canonical_url` + `unique(task, url)` не дадуть дубль-подій.
