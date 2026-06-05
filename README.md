# tg-event-analytics

Configurable framework that turns Telegram posts (via the TeleZip Search API) into
**deduplicated real-world events**, with a Django admin for launching runs, browsing
and filtering results.

"Ethnic clashes in RF" (`AnalysisTask` slug `ethnic-clashes`) is just one *configured*
task — the domain (query, classification schema, tag categories, geo, dedup rules)
lives entirely in the task row, **nothing is hardcoded to it**. The same engine runs a
different theme by creating another task.

> Detailed process description: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## How it works (at a glance)

A run is a **resumable stage-machine**. Each post carries a `stage`; five independent
worker processes each own one stage and pull work from the DB. A long period (a year)
is collected in day-sized chunks and processed as a continuous stream, so a crash or a
TeleZip outage resumes exactly where it stopped — no run is ever restarted from zero.

```
                 CollectChunk queue                Post.stage flow
                 ─────────────────                 ───────────────
   admin ──▶ ResearchRun (collect job)
                 │ enqueue day chunks
                 ▼
   [worker-collect]    TeleZip /Find ───────────▶  collected
   [worker-enrich]     link channel + region ────▶  enriched
   [worker-precluster] content_hash + fuzzy ─────▶  preclustered   (no AI, windowed)
   [worker-classify]   LLM on 1 rep / group ─────▶  classified     (is_relevant + tags + geo)
   [worker-dedup]      LLM judges pair-merges ───▶  deduped/done ─▶ Event
```

Workers run forever and serve **all** active tasks. Stages are decoupled by a
**watermark**: a day is only finalized downstream once its neighbours within the dedup
window are also processed, so cross-day duplicates still merge while the stream keeps
advancing. See the architecture doc for the watermark, claim/lock and windowing logic.

---

## Stack

- Django 5.2 + DRF, **Postgres 16** (everything runs in Docker Compose).
- TeleZip Search API for collection (global concurrency capped in-client, env-driven).
- OpenRouter (`google/gemini-2.5-flash`) for classification + the dedup judge.
- `rapidfuzz` for cheap fuzzy pre-merge; union-find for clustering.
- Admin filters (date-range + faceted multiselect + select2 autocomplete) reused from `pso`.

Services in `docker-compose.yml`: `db`, `web` (port **8001**), and five workers
`worker-collect / -enrich / -precluster / -classify / -dedup` (share the web image via
the `x-worker` anchor). Postgres is on host port **5433** (5432 is taken by `pso`).

---

## Quick start (Docker)

```bash
cp .env.example .env          # fill TELEZIP_API_KEY, OPENROUTER_API_KEY, TELEZIP_MAX_CONCURRENCY=2
make dev                      # build + start db, web, all 5 workers
make migrate
make superuser
make seed                     # seed regions, tag categories, ethnic-clashes task
open http://localhost:8001/admin/
```

Common Make targets (`make help` for the full list):

| target | what it does |
|---|---|
| `make dev` / `make start` / `make stop` / `make restart` | bring the dev stack up/down |
| `make logs` / `make ps` | follow logs / list containers |
| `make workers` / `make workers-logs` / `make workers-stop` | manage the 5 stage workers |
| `make scale-workers ENRICH=2 CLASSIFY=2` | run N replicas of a stage |
| `make worker STAGE=collect` | single ad-hoc pass of one stage |
| `make migrate` / `make makemigrations` / `make superuser` | Django management |
| `make shell` / `make dbshell` | Django / psql shell |
| `make seed` | seed regions + tag categories + the ethnic task |
| `make backup` / `make restore` / `make list-backups` | Postgres dump/restore |
| `make prod` | build + start the prod stack (`docker-compose.prod.yml`) |

---

## Launching a run

From the **admin** (preferred — resumable, long periods):

1. `AnalysisTask` → pick your task → action **"▶ Зібрати за період задачі"** (enter from/to dates).
   This creates a `ResearchRun` (the collect job) and enqueues `CollectChunk`s.
2. The workers pick it up automatically and the stream advances:
   `collected → enriched → preclustered → classified → deduped → done`.
3. Watch progress in `ResearchRun` (`chunk_progress`), `Post` (filter by `stage`), and
   results in `Event`.

The workers are always polling, so you can enqueue more periods at any time; posts are
unique per `(task, url)` and accumulate continuously across runs (Model A).

---

## Browsing results

`/admin/analysis/event/` — events with **faceted filters**: only tag values present in
the current selection are shown, each with a count. Filters are built dynamically per
`TagCategory` (nationality, status, religion, role, group, conflict type, …) plus a
select2 autocomplete for the RF subject. Post links inside an event are sorted by
channel subscribers (reach) descending.

`/admin/analysis/post/` — raw posts with stage filter and job-period filter.
`/admin/analysis/researchrun/` — collect jobs with chunk progress.
`/admin/analysis/tagcategory/` — the shared tag-category registry (key/label/closed/hint).

---

## Reusing the engine for another theme

Everything domain-specific is a field on `AnalysisTask`; vocabularies (`Tag`, `Region`
and their aliases) are **shared across tasks** so canonicalization improves globally.

To add a new theme, create a task with:

- `telezip_query`, `languages`, `search_posts/comments`, `collect_chunk_days`, `telezip_unique`
- `classify_system_prompt` (DOMAIN rules only — the JSON schema is auto-generated)
- `tag_categories` (M2M): which categories the classifier collects for this task
- `closed_tag_categories`: which of those are seed-list-only (match-or-drop) vs open vocab
- `geo_enabled`: off for non-geographic themes (no region/settlement)
- `dedup_window_days`, `dedup_pre_thresh`, `dedup_cand_thresh`
- `dedup_judge_prompt` and `generic_sides` (umbrella terms not counted as a shared signal)

The classifier prompt and JSON schema are assembled from these at run time by
`build_classify_prompt(task)` — same structure, different theme.
