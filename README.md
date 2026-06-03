# tg-event-analytics

Configurable framework that turns Telegram posts (via the TeleZip Search API) into
**deduplicated real-world events**, with an admin UI for browsing and filtering.

"Ethnic clashes in RF" is one configured `AnalysisTask` — nothing is hardcoded to it.

## Pipeline

```
collect    TeleZip /Find (all reposts)            -> Post
enrich     channel metadata + LLM region hint     -> Channel
precluster content_hash + fuzzy text (windowed)   -> Post.dedup_group   (no AI)
classify   LLM on ONE representative per group     -> is_relevant + region/sides/type/summary
dedup      LLM judges candidate group-pairs        -> Event              (window 2d)
aggregate  breakdowns by month/region/type/sides   -> ResearchRun.stats
```

Reference data (`Nationality`, `ConflictType`, `Region`) is **canonicalized by LLM**
(open vocabulary + alias mapping) so there are no semantic duplicates.

## Stack

Django 5 + DRF + Postgres/SQLite. Telegram account manager (Telethon `StringSession`)
reused from llm-council. Admin filters (date-range + multiselect + autocomplete)
reused from the `pso` project.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in TELEZIP_API_KEY, OPENROUTER_API_KEY, ...
cd backend
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py createsuperuser
../.venv/bin/python manage.py seed_regions
../.venv/bin/python manage.py seed_ethnic_clashes
../.venv/bin/python manage.py runserver
```

## Run an analysis

```bash
python manage.py run_analysis ethnic-clashes --from 2025-01-01 --to 2025-12-31
```

Results land in the admin: `/admin/analysis/event/` (filter by period, task, run,
RF subject, nationality, channel) and `/admin/analysis/researchrun/` (aggregate stats).
