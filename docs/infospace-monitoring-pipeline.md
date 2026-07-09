# Моніторинг інформаційного простору (infospace) — архітектура

> **Статус: ПРОЄКТ (дизайн), не реалізовано.** Дата: 2026-07-07.
> Мета документа — зафіксувати архітектуру нового конвеєра до початку кодування.
> Рішення, ухвалені із замовником: полінг **5–15 хв**; подія «жива» (опис
> **оновлюється** при суттєвих доповненнях); вікно збігу — **ковзні 24 год**;
> **кілька тематичних задач** над спільним пулом джерел.

## 0. Що це (один абзац)

Безперервний (не період-жоб, як ResearchRun) конвеєр: **джерела** (Telegram-канали
через власні акаунти, RSS-стрічки, новинні сайти зі скрапінгом; згодом VK) →
сирі **Post** з прив'язкою до джерела → **AI-фільтр** релевантності за темою
задачі → **зіставлення з подіями** останніх 24 год (той самий факт → приєднати
пост і за потреби оновити опис; інший факт → нова подія) → **Event** із коротким
описом. Уся наявна аналітика (графіки, матриця, метрики) працює безкоштовно,
бо результат матеріалізується в ту саму модель `Event`.

## 1. Місце в системі та інваріанти

- Новий конвеєр: **`AnalysisTask.pipeline = "infospace"`** (третє значення поруч
  із `events` і `monitor`). Воркери бачать лише «свої» задачі — правило те саме,
  що для `mon_*`: стадії з префіксом `info_` → задачі pipeline="infospace".
- **Природа Event для infospace: N постів → 1 подія** (як events-конвеєр), але
  дедуп іде **проти живих подій у ковзному вікні 24 год**, а не в батч-вікні
  дедупу. Опис події **оновлюваний** (див. §6) — це нове; для events/monitor
  описи незмінні.
- `Post` реюзається (нові стадії `info_*`), `Event` реюзається (одне нове
  денормалізоване поле `last_post_at`). **Ніяких паралельних «своїх» табличок
  подій** — Event лишається єдиною одиницею аналітики.
- Джерела — **глобальний довідник** (`Source`), до задач підключаються через
  M2M-таблицю з метаданими (`SourceSubscription`) — дзеркалить патерн
  `Channel` ↔ `MonitorChat`. Один збір живить кілька тем; скрін і події — свої
  в кожної задачі.

## 2. Потік

```
Source (telegram | rss | web [| vk])          ← довідник, розклад, health
   │ worker info-collect: claim джерела за next_poll_at, адаптер → RawItem[]
   ▼
фан-аут на активні підписки задач → upsert Post(task, url) stage=info_collected
   │ worker info-screen: дешева LLM за промптом задачі →
   │   {relevant, signature, summary, region} → нерелевантні одразу done
   ▼
Post stage=info_screened (classification = результат скріну)
   │ worker info-event: кандидати-події ±24h (регіон → пріоритет) →
   │   fuzzy top-K → LLM-суддя: ATTACH / ATTACH+UPDATE / NEW
   ▼
Event (approved): приєднаний пост / оновлений summary / нова подія
   → Post stage=done
```

Watermark (як в events-конвеєрі) тут **не потрібен**: вікно збігу ковзне і
рахується відносно дати поста, а не «дозрілості» днів, тому неупорядкована
обробка безпечна.

## 3. Модель даних

### 3.1 Нова: `Source` — джерело інформації

```python
class Source(models.Model):
    KIND_CHOICES = [
        ("telegram", "Telegram-канал (акаунт)"),
        ("rss", "RSS-стрічка"),
        ("web", "Сайт (скрапінг)"),
        ("vk", "VK (згодом)"),          # kind зарезервовано, адаптера поки нема
    ]
    kind = CharField(choices=KIND_CHOICES, db_index=True)
    name = CharField(max_length=200)            # людська назва («Кавказ.Реалії»)
    url = CharField(max_length=500)             # канонічний ідентифікатор:
                                                # tg: @username | t.me/…
                                                # rss: URL стрічки
                                                # web: URL лістинг-сторінки розділу
    region_subject = FK(Region, null=True)      # регіон джерела; денормалізується
                                                # в Post (per-100k працює як усюди)
    language = CharField(blank=True)

    # web-скрапінг
    scraper_key = CharField(blank=True)         # ключ у реєстрі SCRAPERS;
                                                # "" = автоекстракція (trafilatura)
    config = JSONField(default=dict)            # селектори/headers/max_items/
                                                # backfill_limit/full_text/rate…

    # telegram
    tg_account = FK("accounts.TelegramAccount", null=True)  # null = пул (round-robin)

    # розклад і health (пише лише worker info-collect)
    is_active = BooleanField(default=True)
    poll_interval_sec = PositiveIntegerField(default=600)   # 10 хв
    next_poll_at = DateTimeField(db_index=True)             # черга полінгу
    locked_at = DateTimeField(null=True)                    # claim воркером
    state = JSONField(default=dict)             # watermark адаптера:
                                                # tg: {"last_msg_id": N}
                                                # rss: {"etag":…, "modified":…}
    last_ok_at = DateTimeField(null=True)
    last_error = TextField(blank=True)
    consecutive_failures = PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [UniqueConstraint(fields=["kind", "url"])]
```

`state` **непрозорий для всіх, крім адаптера свого kind** — це його приватний
watermark. Health-поля — операційні, редагувати руками не можна (крім
`next_poll_at=now` через адмін-дію «Опитати зараз»).

### 3.2 Нова: `SourceSubscription` — підписка задачі на джерело

```python
class SourceSubscription(models.Model):
    task = FK(AnalysisTask, related_name="source_subscriptions")
    source = FK(Source, related_name="subscriptions")
    is_active = BooleanField(default=True)      # виключити з наступних зборів,
                                                # не втрачаючи історію
    priority = PositiveSmallIntegerField(default=100)
    notes = TextField(blank=True)
    created_at = auto_now_add

    class Meta:
        unique_together = [["task", "source"]]
```

Повний аналог `MonitorChat` (task, channel) — та сама роль, інший тип джерела.
Пост створюється **окремо на кожну підписану задачу** (`Post.task` FK,
unique `(task, url)` — існуючий інваріант №3 не порушується). Це свідома
денормалізація: у кожної теми свій скрін, свій `is_relevant`, свої події.

### 3.3 Зміни в `Post` (адитивні)

- `source = FK(Source, null=True, related_name="posts")` — джерело; для
  events/monitor-постів лишається NULL.
- `title = CharField(max_length=500, blank=True)` — заголовок статті/RSS-item;
  для Telegram порожній.
- Нові стадії в `STAGE_CHOICES` (влазять у `max_length=16`):
  - `STAGE_INFO_COLLECTED = "info_collected"`
  - `STAGE_INFO_SCREENED = "info_screened"`
- Решта реюзається як є: `url`, `text`, `content_hash`, `posted_at`,
  `region_subject` (← від Source при вставці — тримаємо денормалізацію,
  граблі №4 з AI-GUIDE), `classification` (результат скріну), `is_relevant`,
  `event`, claim-поля стадій.
- Для telegram-джерел заповнюємо і `channel` (FK на існуючий `Channel`,
  створюємо при першому полінгу) — канальна аналітика/reach працює.
  Для rss/web `channel=NULL`, `channel_name = source.name` (щоб списки/графіки
  по каналах щось показували).

### 3.4 Зміни в `AnalysisTask` (адитивні)

- `PIPELINE_INFOSPACE = "infospace"` у `PIPELINE_CHOICES`.
- Нові поля конфігурації (порожньо = дефолт із `infospace/prompts.py` — той
  самий патерн, що prescreen/tagger; редагується з адмінки, групується
  окремою карткою етапів «🛰 Моніторинг інформпростору»):
  - `info_screen_model` (дешева OpenRouter-модель; порожньо = дефолт settings),
  - `info_screen_prompt` (системний промпт скріну: критерії теми + JSON-схема),
  - `info_judge_prompt` (системний промпт судді збігу подій),
  - `info_match_window_hours = PositiveSmallIntegerField(default=24)`,
  - `info_update_summaries = BooleanField(default=True)` — «жива» подія.
- Події створюються одразу `review_status=approved` (як у monitor: валідацію
  виконав LLM-контур). Дворівневий аудит (авто-review + агент-аудит) можна
  увімкнути пізніше — механіка вже існує, треба лише розширити фільтр задач
  review-воркера (Phase 4).

### 3.5 Зміни в `Event` (одне поле)

- `last_post_at = DateTimeField(null=True, db_index=True)` — час останнього
  приєднаного поста. Заповнює **лише** info_event-стадія; інші конвеєри не
  чіпають. Потрібно для: (а) швидкого відбору кандидатів у вікні 24 год без
  `MAX(posts.posted_at)` на мультимільйонній таблиці; (б) сортування «живі
  сюжети зверху» в адмінці.

Семантика лічильників для infospace-подій: `channel_count` = **кількість
унікальних джерел** (кросджерельна підтвердженість — головна метрика довіри),
`reach` = сума підписників лише TG-каналів події (для rss/web підписників
нема; у звітах по infospace дивитись на count/джерела, не reach — та сама
пересторога, що для monitor-reach у граблях AI-GUIDE).

### 3.6 Теги

Phase 1 — **без тегів** (подія = дата+регіон+опис+джерела). Коли задачі
знадобляться теми-теги: додаємо категорії у `task.tag_categories`, скрін-промпт
розширюється списком категорій, і **обов'язково створюємо рядки `TagCategory`**
(інваріант №6 AI-GUIDE, інакше `?e=1` в адмін-фасеті).

## 4. Спільний інтерфейс джерела (адаптери)

Новий пакет `backend/analysis/services/infospace/`:

```
infospace/
  __init__.py
  adapters/
    __init__.py        # реєстр ADAPTERS: kind → клас (telegram, rss, web)
    base.py            # RawItem + BaseSourceAdapter (контракт)
    telegram.py        # полінг історії каналу акаунтом (Telethon)
    rss.py             # feedparser + умовний GET (etag/modified)
    web.py             # discovery (лістинг) + extraction (trafilatura/селектори)
  scrapers.py          # реєстр кастомних скраперів SCRAPERS: key → клас
  stages.py            # info_collect_once / info_screen_once / info_event_once
  prompts.py           # дефолтні INFO_SCREEN_PROMPT / INFO_JUDGE_PROMPT
```

### 4.1 Контракт

```python
@dataclass
class RawItem:
    external_id: str            # msg_id | guid | канонічний url
    url: str                    # канонічний лінк (utm/fragment зрізані)
    title: str                  # "" для telegram
    text: str
    posted_at: datetime | None  # UTC; None → now + позначка в meta
    author: str
    meta: dict                  # сирі дані адаптера (views, enclosures…)

class BaseSourceAdapter:
    kind: str

    def fetch(self, source: Source) -> list[RawItem]:
        """Повертає ЛИШЕ НОВІ елементи відносно source.state (watermark).
        Мутує source.state У ПАМ'ЯТІ; збереження в БД робить стадія ПІСЛЯ
        успішного upsert постів (щоб збій не загубив елементи)."""
```

Правила контракту (закріплюються спільним параметризованим контракт-тестом
по всьому реєстру `ADAPTERS`):

1. `fetch` **не пише в БД** — вся персистенція в стадії. Тестується без Django-магії.
2. Ідемпотентність: повторний виклик із тим самим `state` не дає дублів;
   друга лінія захисту — upsert по unique `(task, url)`.
3. Мережеві/парс-помилки → виняток; backoff і health рахує стадія, не адаптер.
4. Ліміт елементів за виклик: `config.max_items` (дефолт 100). Перший полінг
   (порожній `state`) — окремий `config.backfill_limit` (дефолт: останні 20
   елементів), щоб не залити в конвеєр роки історії.
5. Канонізація URL — спільна утиліта `canonical_url()` (зрізає `utm_*`,
   `#fragment`, приводить хост до нижнього регістру): той самий матеріал із
   RSS і зі скрапінгу сайту схлопується в один Post.

### 4.2 `telegram` — полінг історії акаунтом

- `Source.url` = `@username` / `t.me/...`. Акаунт: `source.tg_account`, а якщо
  NULL — простий round-robin по активних `TelegramAccount(is_authenticated=True)`.
- Механіка: `client.iter_messages(entity, min_id=state.last_msg_id,
  limit=max_items)` → reverse (старіші перші); `state.last_msg_id` = максимум.
  Перший полінг — `backfill_limit` останніх повідомлень.
- Реюз: розширюємо `accounts.services.telegram_client.TelegramUserClient`
  методом `fetch_history(account, handle, min_id, limit)` (поруч із наявними
  `get_channel_meta`/`get_message_date`); при першому полінгу створюємо/лінкуємо
  `Channel` через наявний `get_channel_meta`.
- **`auto_join=False` за замовчуванням**: публічні канали читаються без вступу
  (менший бот-слід на акаунті). Приватні — вступ вручну або `config.auto_join=true`
  свідомо на конкретному джерелі.
- `FloodWaitError(e.seconds)` → `next_poll_at = now + seconds + jitter`,
  **без** інкременту failures (це не збій, це ліміт).
- Ввічливість до акаунта: пауза 1–2 с між джерелами одного акаунта в межах
  проходу воркера.

### 4.3 `rss`

- `feedparser`; умовний GET: `state.etag`/`state.modified` → 304 = порожній
  результат задарма.
- `posted_at` із `published`/`updated`; нема — `now` + `meta["date_missing"]=true`.
- RSS часто дає лише анонс: `config.full_text=true` → дотягнути повний текст
  статті екстрактором по `item.link` (той самий extraction-код, що у web-адаптера).

### 4.4 `web` — скрапінг сайтів

Два кроки, обидва конфігуруються з БД (без деплою на новий сайт):

1. **Discovery** — знайти лінки статей на лістинг-сторінці (`Source.url`):
   `config.link_selector` (CSS) або `config.link_pattern` (regex по href);
   лінки обмежуються доменом джерела, максимум `max_items`.
2. **Extraction** — витягнути статтю за лінком:
   - дефолт: **trafilatura** (сам знаходить заголовок/текст/дату — для
     більшості новинних сайтів нуль конфігурації);
   - override: `config.selectors = {"title":…, "body":…, "date":…}` для
     сайтів, де автоекстракція бреше;
   - крайній випадок: `scraper_key` → кастомний клас у реєстрі `SCRAPERS`
     (`infospace/scrapers.py`) — довільний Python (нестандартні API, пагінація).
     JS-rendered/анти-бот сайти (Playwright) — **свідомо поза** Phase 1–2.
- Ввічливість: власний User-Agent, timeout 20 с, ≤1 запит/с на домен,
  поважаємо явні заборони.
- Watermark не потрібен: дедуп через unique `(task, url)`; `state` лишається
  порожнім (хіба кастомний скрапер щось своє зберігає).

### 4.5 `vk` (Phase 4)

`kind="vk"` зарезервований у choices. Реалізація ляже в той самий контракт:
`wall.get` через API-токен (крашче) або скрапінг. Окреме питання — токени і
їх зберігання (аналог TelegramAccount або поле config) — вирішимо на Phase 4.

## 5. Стадії конвеєра (`infospace/stages.py`)

Реєструються в `run_worker.ALL_RUNNERS`; роутінг задач у `run_worker._tasks`:
`stage.startswith("info_")` → `pipeline="infospace"`.

### 5.1 `info_collect` — полінг джерел (**taskless**)

Єдина стадія, що працює не по задачах, а по **джерелах** (одне джерело живить
кілька задач — полінг мусить бути один). `run_worker` отримує мінімальне
розширення: множина `TASKLESS_STAGES = {"info_collect"}`, для яких цикл
`for task in tasks` замінюється на `while runner(): …`.

Один прохід `info_collect_once()`:

1. **Claim джерела**: `select_for_update(skip_locked=True)` по
   `Source(is_active=True, next_poll_at<=now, locked_at is NULL|stale)`,
   що має ≥1 активну підписку активної infospace-задачі; ставимо `locked_at`.
2. `items = ADAPTERS[source.kind]().fetch(source)`.
3. **Фан-аут**: для кожної активної `SourceSubscription` →
   `Post.objects.update_or_create(task=…, url=canonical_url, defaults=…)`
   зі `stage=info_collected`, `source`, `title`, `text`, `posted_at`,
   `content_hash`, `region_subject=source.region_subject`, `channel` (tg).
   Уже наявний пост (будь-якої стадії) **не відкочується** назад у
   info_collected — update лише метаданих (повтор ідемпотентний).
4. **Успіх**: зберегти `source.state`, `last_ok_at=now`, `failures=0`,
   `next_poll_at = now + poll_interval ± 10% jitter`, `locked_at=NULL`.
5. **Збій**: `last_error`, `consecutive_failures += 1`,
   `next_poll_at = now + poll_interval * 2^failures` (cap 6 год), `locked_at=NULL`.
   Джерело **не** вимикається автоматично — бекоф і так розріджує спроби;
   у адмінці світиться health-бейдж.

**Обмеження: 1 репліка воркера** — одна Telethon StringSession не терпить
паралельних конекшенів із різних процесів (та сама грабля, що була з TeleZip
до слотів). Масштабування пізніше — шардінг джерел по акаунтах.

### 5.2 `info_screen` — AI-фільтр релевантності (по задачах)

1. `_claim_posts(task, "info_collected", limit≈10)` — наявна claim-механіка.
2. На пост — один виклик **дешевої** моделі (`task.info_screen_model`,
   OpenRouter через наявний `services/llm.py`) з `task.info_screen_prompt`.
   Відповідь — строгий JSON:
   ```json
   {"relevant": true, "reason": "…",
    "signature": "один рядок: хто/що/де — канонічний підпис факту",
    "summary": "2–3 речення для картки події",
    "region": "Дагестан | null"}
   ```
   Скрін і екстракція навмисно **в одному виклику** (економія: не платимо
   двічі за той самий вхідний текст).
3. `region` (якщо `task.geo_enabled` і модель щось дала) → `resolve_region()`
   з `normalize.py`; перекриває регіон джерела. Інакше лишається
   `source.region_subject`.
4. Запис: `is_relevant`, `classification={signature, summary, screen_reason…}`;
   нерелевантні → одразу `stage=done` (короткий шлях, як antiscope у
   precluster); релевантні → `stage=info_screened`.
5. Збої LLM: `stage_attempts += 1`, після 3 → `failed` (транзієнти пізніше
   можна пере-чергувати — патерн mon-failed уже є).

### 5.3 `info_event` — зіставлення з подіями (по задачах)

Серце флоу. Обробляє **по одному посту** за прохід (серіалізація створення
подій), під `pg_advisory_xact_lock(task_id)` — два воркери/репліки ніколи не
створять дубль-подію з двох одночасних постів про той самий факт.

1. Claim один `Post(stage=info_screened)`.
2. **Кандидати**: `Event.objects.filter(task=task,
   last_post_at__range=[post.posted_at - W, post.posted_at + W])`, де
   `W = task.info_match_window_hours` (24). Вікно рахується **відносно дати
   поста** (не `now`) — бекфіл і відставання обробки матчаться коректно.
   Якщо у поста є `region_subject` — спершу кандидати того ж регіону, потім
   без регіону (події без гео).
3. **Скоринг**: `rapidfuzz.token_set_ratio(post.signature, event.summary)`;
   top-K=5 з ratio ≥ 45 йдуть судді. Нема кандидатів → одразу NEW (без LLM).
4. **Суддя** (`task.llm_model`, промпт `task.info_judge_prompt`): бачить
   title+text поста (обрізаний) і список кандидатів (id, дата, опис). Вердикт:
   ```json
   {"verdict": "attach" | "new",
    "event_id": 123,
    "update_summary": true,
    "new_summary": "оновлений опис, якщо є суттєві доповнення"}
   ```
5. **ATTACH**: `post.event = E`; перерахунок `post_count`,
   `channel_count` (= унікальні джерела), `reach` (лише TG-канали),
   `last_post_at = max(…, post.posted_at)`. Якщо `update_summary` і
   `task.info_update_summaries` → `E.summary = new_summary` («жива» подія).
6. **NEW**: `Event(task, event_date=post.posted_at.date(), region_subject,
   summary=classification.summary, review_status=approved,
   last_post_at=post.posted_at)`.
7. `post.stage = done`.

Ланцюг багатоденного сюжету при ковзному вікні: подія «живе», поки в неї
прилітають пости частіше ніж раз на 24 год (вікно від `last_post_at`);
затихла на добу+ → наступний пост відкриє нову подію. Це усвідомлений
компроміс ковзного вікна (обрано замовником).

## 6. «Жива» подія — правила оновлення опису

- Опис оновлює **лише** суддя info_event і **лише** при
  `task.info_update_summaries=True`.
- Оновлення = переписаний повний опис (не append) — суддя отримує старий опис
  і новий пост, повертає консолідований текст ≤ 3 речень.
- `event_date` НЕ змінюється (дата першого поста) — денна аналітика стабільна.
- Історія правок не зберігається (Phase 1); якщо знадобиться — окреме питання
  (простий варіант: лог у `review_notes`).

## 7. Воркери та compose

```yaml
worker-info-collect: { <<: *worker, command: python manage.py run_worker --stage info_collect }  # 1 репліка!
worker-info-screen:  { <<: *worker, command: python manage.py run_worker --stage info_screen }   # масштабується
worker-info-event:   { <<: *worker, command: python manage.py run_worker --stage info_event }    # advisory lock
```

Зміни в `run_worker.py`: реєстрація ранерів, `TASKLESS_STAGES`, роутінг
`info_` → pipeline="infospace". Після змін коду — `docker compose restart
worker-info-…` (граблі «воркери не перечитують код»).

## 8. Адмінка

- **Source changelist**: колонки kind, name, регіон, health-бейдж
  (🟢 ok / 🟡 failures 1–2 / 🔴 ≥3 + last_error у tooltip), `last_ok_at`,
  `next_poll_at`, постів за 24 год (annotate). Фільтри: kind, health, регіон,
  задача-підписник. Дії:
  - **«Опитати зараз»** — `next_poll_at=now`;
  - **«Тестовий збір (dry-run)»** — виконати `fetch` без запису, показати
    перші N RawItem (налагодження селекторів без сміття в БД);
  - активувати/деактивувати.
- **SourceSubscription** — інлайн на формі задачі (як MonitorChat) і інлайн
  на формі Source (видно, які теми споживають джерело).
- **Форма задачі**: нова картка етапів «🛰 Моніторинг інформпростору»
  (JS-механіка показу «своїх» етапів конвеєра вже є — доїхати третій pipeline).
- **Event changelist**: без змін; для infospace-задач корисна колонка/сортування
  `last_post_at` («живі сюжети зверху»).

## 9. Промпти

- Дефолти в `infospace/prompts.py`; робочі копії — **в полях задачі**
  (канон проєкту: порожнє поле = дефолт із коду).
- Канон v3: англійська інструкція, строгий JSON, визначення критеріїв теми
  дослівно. `INFO_SCREEN_PROMPT` — шаблон із явним місцем під формулювання
  теми задачі; `INFO_JUDGE_PROMPT` — «той самий факт чи інший?» з правилом:
  уточнення/деталі/цифри = той самий факт (attach), інша дія/місце/учасники =
  нова подія; окреме поле вердикту — чи є суттєві доповнення для оновлення опису.

## 10. Тести (закладаємо інфраструктуру проєкту)

Зараз тестів немає взагалі — цей флоу заходить разом із тест-інфраструктурою,
яка стає патерном для всього проєкту.

**Стек**: `pytest`, `pytest-django`, `factory-boy`, `respx` (мок httpx),
`freezegun` (час у тестах вікон/бекофу). Конфіг: `pytest.ini` +
`backend/conftest.py`; тести в `backend/analysis/tests/`
(`test_adapters_*.py`, `test_infospace_stages.py`, `factories.py`,
`fixtures/` — sample.rss.xml, listing.html, article.html).
**Запуск**: `docker compose exec web pytest` (Postgres із compose, test-БД
створюється сама, `--reuse-db` для швидкості).
**LLM у тестах**: фікстура-фейк, що підміняє виклик у `services/llm.py`
детермінованим JSON. **Telethon у тестах**: фейк-клієнт зі списком
повідомлень; адаптер приймає client-фабрику (DI).

Матриця покриття:

| Блок | Кейси |
|---|---|
| `canonical_url` | utm/fragment/хост-регістр; ідентичність rss- і web-версії лінка |
| adapters/rss | парсинг фікстури; etag→304 (нуль елементів); відсутні дати; guid-дедуп; full_text-дотяжка (respx) |
| adapters/web | discovery по селектору і по regex; чужі домени відсікаються; trafilatura-екстракція (мок); селектор-override; 404/timeout → виняток |
| adapters/telegram | watermark min_id; перший полінг = backfill_limit; FloodWait → next_poll_at без failure; порожній канал |
| контракт адаптерів | параметризовано по ADAPTERS: поля RawItem, відсутність запису в БД, ліміти |
| info_collect | claim/stale-reclaim; фан-аут на 2 задачі; ідемпотентний повтор (unique task,url); state зберігається ЛИШЕ після успішного upsert; backoff-прогресія і cap; jitter у межах |
| info_screen | relevant → info_screened + classification; irrelevant → done; битий JSON → attempts → failed; регіон: модель ⊕ fallback джерела |
| info_event | NEW без кандидатів (без виклику судді); ATTACH + лічильники (post_count/джерела/reach/last_post_at); ATTACH+UPDATE оновлює summary; update вимкнено полем задачі → не оновлює; межі вікна ±24h від дати поста; пріоритет кандидатів свого регіону; серіалізація: два пости про один факт → 1 подія |
| міграції/моделі | unique (kind,url), (task,source); нові stage-choices не ламають старі конвеєри |

## 11. Нові залежності

```
feedparser>=6.0          # rss
trafilatura>=1.9         # автоекстракція статей (тягне lxml)
selectolax>=0.3          # швидкий CSS-парсер для discovery/селекторів
httpx>=0.27              # sync http в адаптерах (вже транзитивний через openai)
# dev:
pytest, pytest-django, factory-boy, respx, freezegun
```

Нових env-змінних не треба: TG api_id/hash уже в settings, OpenRouter-ключі є.

## 12. Фази впровадження

| Фаза | Обсяг | Результат |
|---|---|---|
| **0. Скелет** | бекап БД → міграції (Source, SourceSubscription, Post.source/title/стадії, поля задачі, Event.last_post_at); реєстри адаптерів/скраперів; run_worker-роутінг; тест-інфраструктура (pytest піднімається, перший тест зелений) | фундамент |
| **1. RSS end-to-end** | rss-адаптер + info_collect + info_screen + info_event + адмінка Source + промпти-дефолти + тести блоку | робочий контур на 3–5 стрічках: від стрічки до події в адмінці |
| **2. Web-скрапінг** | web-адаптер (discovery + trafilatura + селектори + реєстр кастомних), dry-run дія в адмінці, тести | новинні сайти |
| **3. Telegram** | fetch_history у TelegramUserClient, tg-адаптер, пул акаунтів, FloodWait, Channel-лінк, тести | канали через акаунти |
| **4. Розширення** | VK-адаптер; авто-аудит подій (реюз review-воркера); теги-теми (TagCategory!); ретеншн нерелевантних постів; звіти по джерелах | за потребою |

Кожна фаза мержиться окремо і не чіпає events/monitor-конвеєри.

## 13. Ризики та граблі (нові, специфічні для флоу)

- **TG-акаунти — головний ризик.** Полінг десятків каналів = FloodWait/бан:
  інтервали ≥10 хв, jitter, пауза між каналами, читання публічних без join,
  під моніторинг — **окремі акаунти, не особисті**. Бан акаунта = health-бейдж
  на всіх його джерелах, ручна заміна акаунта.
- **Одна StringSession з двох процесів** = конфлікт сесії → строго 1 репліка
  info-collect (записано в compose коментарем).
- **Скрапери ламаються тихо** (редизайн сайту): trafilatura почне віддавати
  сміття або discovery — нуль лінків. Захист: health-бейджі, «постів за 24 год»
  у списку джерел, dry-run дія.
- **Вартість LLM**: скрін = 1 виклик × пост × задачу-підписника. Контроль:
  дешева модель, `max_items`/`backfill_limit` на джерело, моніторинг обсягу
  (постів/добу в адмінці).
- **Ковзне вікно склеює довгі сюжети** в одну подію, поки потік постів не
  переривається на 24 год — для денних графіків це «1 подія з N постами»,
  а не «N подій по днях». Усвідомлений вибір; якщо заважатиме — перемикач
  режиму вікна на задачі (Phase 4).
- **`update_or_create` на гарячій таблиці Post** — фан-аут тримати в коротких
  транзакціях, батчити по джерелу, не по всій черзі.
- Перед міграціями — **бекап** (`pg_dump` у `backups/`, правило проєкту).

## 14. Відкриті питання (не блокують Phase 0–1)

1. **Список джерел**: чи є вже перелік каналів/стрічок/сайтів? Потрібна
   сідер-команда `import_sources` (CSV/JSON) — формат узгодимо, коли буде список.
2. **Мови**: джерела рос/укр/нацмовами республік — чи відсікати за мовою на
   скріні, чи тема сама відфільтрує?
3. **Обсяг**: скільки джерел на старті (10? 100?) — впливає на вибір моделі
   скріну і кількість реплік info-screen.
4. **Аудит infospace-подій**: лишаємо approved одразу чи вмикаємо авто-review
   з Phase 1?
5. **Ретеншн**: нерелевантні done-пости зберігаємо (налагодження промптів)
   чи чистимо за N днів?
6. **VK**: офіційний API з токеном чи скрапінг? Де зберігати токени?
7. **Сповіщення**: чи потрібен алерт (телеграм-бот/пошта), коли джерело
   червоніє або з'являється гучна подія (багато джерел за годину)?
