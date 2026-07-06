# AI-GUIDE — модуль аналізу подій (точка входу для ШІ)

> **Призначення:** швидко відновити контекст по системі. Читай ЦЕЙ файл першим;
> глибші деталі — за посиланнями внизу. Стан на 2026-07-05 (гілка `main`).

## 0. Що це за система (один абзац)

Django 5.2 + Postgres (`tg_events`, усе в Docker, адмінка на **:8001**) аналізує
російський Telegram (джерело даних — пошуковий API **TeleZip**) по нацреспубліках РФ.
Два типи сигналу: **події-інциденти** (репортажі каналів: етнічні сутички, корупція,
протести) і **критика-думки** (коментарі людей у чатах). Після уніфікації 2026-07-05
**обидва типи матеріалізуються в модель `Event`** — вся аналітика (графіки, матриця,
звіти) читає ТІЛЬКИ Event.

## 1. ГОЛОВНІ ІНВАРІАНТИ (порушиш — зламаєш аналітику)

1. **Event — єдина одиниця аналітики.** Графіки/матриця будуються лише по Event.
2. **Дві природи Event, розрізняються `task.pipeline`:**
   - `events`-конвеєр: **N постів → 1 подія** (дедуп; інцидент один — описів багато);
   - `monitor`-конвеєр: **1 коментар → 1 подія, БЕЗ дедупу** — коментар = думка окремої
     людини, метрика = «скільки людей критикує», злиття знищило б лічильник.
     Механіка дзеркала: `monitor_stages.sync_comment_event(post)` (create/update/remove
     за `is_relevant`); викликається з `monitor_ingest_tags` і `monitor_validate`.
3. **`Post` має unique (task_id, url)** — один t.me-пост прив'язується лише до ОДНІЄЇ
   події в межах задачі.
4. **`region_subject`** (FK на Region, є і на Event, і на Post — денормалізовано з
   Channel) — канонічне гео. `Event.region` — сирий текст, ним НЕ фільтрувати
   (граблі: «Саха» матчить «Сахалін»).
5. **Агрегації — тільки через `services/metrics.py`** (`EventSource`/`PostSource`):
   одна per-100k формула від `Region.population`, reach = distinct-канали,
   `people_count = COUNT(DISTINCT COALESCE(author_tg_id, content_hash))`.
   Перед `.values().annotate()` ОБОВ'ЯЗКОВО `.order_by()` — інакше ordering
   changelist-а підмішується в GROUP BY (рядок на кожен об'єкт).
6. **Теги = `Tag(name, category)`** + реєстр **`TagCategory`**. Створюєш нову
   категорію тегів → МУСИШ створити рядок `TagCategory(key=...)`, інакше фасет-фільтр
   адмінки не реєструється і будь-який URL з `tag_<cat>` падає на `?e=1`.
7. **Аудит подій:** `review_status` (`pending/approved/rejected`) — адмінка за
   замовчуванням показує ЛИШЕ approved (`?review_status=all` — явний вибір).
   Події-коментарі створюються одразу `approved` (валідацію вже пройшов агент).

## 2. Задачі (AnalysisTask) зараз у БД

| id | slug | pipeline | що це | подій |
|----|------|----------|-------|-------|
| 1 | `ethnic-clashes` | events | міжетнічні сутички по всій РФ, 2025-2026 | ~9.5k |
| 3 | `republics-criticism-monitor` | monitor | критика влади в коментарях чатів 3 республік (Дагестан/Татарстан/Саха) | ~108.7k (1:1 з коментарів) |
| 6 | `ethnic-tension-events` | events | події напруги у 8 республіках (створюються ad-hoc скриптами, не воркерами) | ~350+ |

task=6 — «контейнер» для дослідницьких конвеєрів; його події створюються скриптами
`_dir/` і тегуються категоріями:
- `ethnic_event`: 2363 C2-протести проти етнодискримінації, 2364 C3-нацрух, 2365 C4-діаспора/культ.форум
- `econ_event`: 2366 E1-суперечки з центром, 2367 E2-корупція, 2368 E3-протести/страйки, 2369 E4-влада про дискримінацію
- `political_event`: 2370 P1-політ.суперечки з центром, 2371 P2-етнопротест проти федполітики, 2372 P3-рос-шовіністи проти місцевої

Регіони (`Region.id`): Башкортостан=3, Бурятія=4, Дагестан=5, Інгушетія=6,
Саха(Якутія)=15, Татарстан=17, Тива=18, Чечня=21. `population` заповнено → per-100k.

## 3. Два конвеєри

### events (docker-воркери, claim-черга по Post.stage)
```
collect(TeleZip) → enrich(Telethon) → precluster(fuzzy) → classify(LLM) → dedup(LLM-суддя) → Event
  → авто-аудит (воркер review, ДЕШЕВА LLM — перший прохід)
  → АГЕНТ-АУДИТ (гібрид): воркер ev-runs готує батчі approved-подій у
    _dir/runs/run_<id>/ → «Чекає агента» → агенти пишуть keep/reject+правки
    (§5 EVENT_REVIEW_PROMPT.md / task.agent_review_prompt) → ранер застосовує.
```
Аудит ДВОЯРУСНИЙ: review_model=gemini-flash це НЕ помилка — дешевий перший
прохід; якість добиває агент-аудит. Деталі стадій: **docs/ARCHITECTURE.md**.

**Реюзабельні рецептури:** форма «Задача аналізу» в адмінці згрупована ПО ЕТАПАХ
обраного конвеєра (картки 📰 Пошук подій / 💬 Моніторинг коментарів; JS ховає чужі
етапи). ВСІ робочі параметри стадій редагуються з адмінки: events — запит/промпт
класифікації/пороги дедупу/промпт судді/аудит (поля існували давно); monitor —
`mon_min_len`/`mon_max_len` (фільтр), `prescreen_model`/`prescreen_prompt`,
`tagger_prompt` (іде агентам у SYSTEM_PROMPT.md батчів). Порожнє поле = дефолт із
`analysis/pilot/prompts.py` — тож існуючі задачі працюють як раніше.

### monitor (критика; збір воркерами, тегування оркеструється Claude-агентами)
```
mon_collect → mon_filter(25..600 симв) → mon_prescreen(OpenRouter, дешеве так/ні)
  → [АГЕНТ: тег+валідація ОДНИМ проходом: criticism_target+topic+opinion ⇒ is_relevant]
  → sync_comment_event: Event 1:1 → done
```
**ГІБРИДНИЙ ЗАПУСК ЧЕРЕЗ UI (канон з 2026-07-06):** адмінка Збори → Додати
(задача+період) → чанки плануються самі → воркери збирають/фільтрують/прескрінять →
воркер `mon-runs` (`services/pipeline_runs.py`) готує батчі в `_dir/runs/run_<id>/`
→ статус **«Чекає агента»** (жовтий блок на Збори→Статус з інструкцією) → людина
каже Claude Code «протегуй батчі запуску #N» (Haiku-агенти, SYSTEM_PROMPT.md у теці)
→ ранер сам бачить `*_done.json`, інжестить, створює події 1:1, закриває запуск.
Ранер сам пере-чергує mon-failed пости (транзієнтні LLM-збої) до 3 разів.
Команди під капотом: `monitor_prepare_batches --require-prescreen`, `monitor_ingest_tags`.
Промпти: `analysis/pilot/prompts.py`. Деталі: **docs/comments-analysis-pipeline.md**.
Разовий перенос історії: `_dir/materialize_comment_events.py` (ідемпотентний).

### ad-hoc дослідницькі конвеєри (task=6)
```
локальні канали республіки → TeleZip '*' unique по каналу-місяцю (кеш econ_raw_<key>.json!)
  → keyword AND-фільтр → Sonnet-класифікація (гео=республіка + справжня подія)
  → групування дублів в інциденти + адверсаріальний аудит → Event(+Post-джерела, approved)
```
Деталі+промпти: **docs/ethnic-events-pipeline.md**, **docs/econ-events-pipeline.md**.
Кеш `_dir/econ_raw_<key>.json` (~152k сирих постів 8 республік за 2026) — нові
колонки фільтруй офлайн, БЕЗ повторного TeleZip.

## 4. Аналітичний шар

### services/metrics.py — спільний контракт
`EventSource(qs, gran)` / `PostSource(qs, gran)`; методи: `count`, `by_region`
(count/reach/per_100k), `republic_totals`, `republic_timeseries`, `by_tag`,
`tag_by_region`, `tag_timeseries`, `timeseries`; у PostSource ще `people_count`.
PostSource сам додає краї: `is_relevant=True`, `exclude(is_channel_repost=True)`.

### Адмінка (усе на Event changelist)
- **`#charts`** (вбудований режим списку подій; окремої сторінки НЕМає, ендпоінт
  `event/charts/` віддає лише фрагмент для JS): події в часі, суб'єкти+per-100k,
  топ каналів, **тег×республіки** (стек), **тег×час** (лінії), **% від усіх
  повідомлень** (знаменник `ChannelDailyStat`, рендериться лише для monitor-задач).
  Один контрол «Категорії» керує всіма трьома тег-секціями.
- **`matrix/`** — матриця 8 республік × індикатори (ФУР/ГЕР/ПОЛ, порядок як у
  Google-таблиці замовника); всі числа наживо з БД, кожне — лінк на відфільтрований
  changelist. Клас-конфіг: `EventAdmin.MATRIX_SECTIONS`.
- **`conflicts/`** — ко-оквіренс матриця національностей (task=1).
- **Фільтри:** «Задача» — одиночний вибір (`?task=<id>`); «Статус аудиту» — дефолт
  «Схвалено» (`ReviewStatusDefaultFilter`); тег-фасети `?tag_<category>=<tag_id>`
  (+`_excl`) — згортаються в `<details>`, відкриті лише активні; регіон —
  `?region_id=<id>`; дати — `event_date__range__gte/lte`.
- Пост-графіки (`post/#charts`) — легасі, лишені свідомо.

## 5. Мапа ключових файлів

```
backend/analysis/
  models.py                 # уся модель даних (коментарі в docstring — актуальні)
  admin.py                  # EventAdmin/PostAdmin: charts_view, matrix_view, фільтри
  services/metrics.py       # MetricSource — ЄДИНЕ місце формул агрегацій
  services/monitor_stages.py# monitor-стадії + sync_comment_event (Event 1:1)
  services/stages.py        # events-стадії воркерів
  services/telezip.py       # TeleZip-клієнт + TelezipSlot (глобальний семафор)
  services/normalize.py     # канонізація тегів/регіонів через аліаси
  pilot/prompts.py          # промпти monitor-конвеєра (single source of truth)
  multiselect_filter.py     # бази кастомних фільтрів (+filter_is_active)
backend/templates/admin/analysis/event/
  _charts_body.html         # ВЕСЬ JS графіків (Chart.js), matrix.html — матриця
backend/_dir/               # host-only (gitignored): ad-hoc скрипти, кеші, батчі
docs/                       # ARCHITECTURE, comments-…, ethnic-…, econ-… pipelines
```

## 6. Граблі (перевірені кров'ю)

- **TeleZip:** негація `-(…)` у запиті = 500/timeout/429 (68× повільніше). Збирай
  `'*'` + `unique=True` по одному каналу; великі періоди — **по днях**, не місяцями.
  DNS у контейнері після падіння VPN: `echo "77.88.192.66 api.telezip.net" >> /etc/hosts`
  (переставляти після рестарту контейнера).
- **Воркери не перечитують код** — після зміни stage-коду `docker compose restart worker-…`;
  web теж рестартуй після зміни admin.py.
- **`docker compose restart` НЕ перечитує .env** — оновив ключі (OpenRouter/TeleZip) →
  `docker compose up -d --force-recreate worker-…`, інакше воркери житимуть зі старим
  env (ловили 401 добу). Легасі-воркер mon-tag ВИДАЛЕНО з compose — тегують агенти.
- **`Post.region_subject`** заповнюється на вставці (mon_collect) — але для events-конвеєра
  вставка своя; після нових шляхів вставки перевіряй денормалізацію (бекфіл-скрипт
  `_dir/backfill_post_region.py` ідемпотентний).
- **Довгі фонові збори** — одразу перевіряй, що процес реально працює (мовчазні провали).
- **Бекапи перед схемними змінами**: `docker compose exec -T db pg_dump -U postgres tg_events | gzip > backups/…`.
  Останній повний: `backups/tg_events_20260704_121716_pre_unify_phase2.sql.gz`.
- **Reach для monitor-подій** = підписники каналу КОЖНОГО коментаря → `Sum(reach)`
  по критиці завищує; для критики дивись count/per-100k, не reach.
- **2026 дані**: свіжі місяці (травень-червень) недозібрані — індекс TeleZip дозріває;
  перезбір місяця відновлює ×4-5 постів (недеструктивний, URL-дедуп).
- **Тегова таксономія росте** (`criticism_target` відкрита) — періодично чистити
  через `monitor_review_tags`.

## 7. Швидка перевірка стану (рецепти)

```bash
# скільки подій по задачах/статусах
docker compose exec -T web python manage.py shell -c \
 "from analysis.models import Event; from django.db.models import Count; \
  print(list(Event.objects.values('task_id','review_status').annotate(n=Count('id'))))"

# рендер сторінок без браузера (Django test client, force_login суперюзера)
# див. патерн у git-історії: /tmp/*.py скрипти через `manage.py shell < file`
```

## 8. Глибші доки

- `docs/ARCHITECTURE.md` — events-воркери, стадії, watermark, failure-семантика.
- `docs/comments-analysis-pipeline.md` — monitor-конвеєр критики + Event 1:1.
- `docs/ethnic-events-pipeline.md` — ad-hoc конвеєр етно-подій (C2/C3/C4).
- `docs/econ-events-pipeline.md` — економічні події E1-E4: keyword-регекси, усі промпти.
- Пам'ять Claude (`~/.claude/projects/...-sm-analytics/memory/`) — операційні уроки.
