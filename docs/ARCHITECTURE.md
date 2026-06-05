# Architecture & process flow

How a long-period run is collected, processed and turned into deduplicated **events**.
This document describes the *processes* (the five stage workers) and the mechanics that
make a year-long run resumable and crash-safe: claim-based queues, the watermark, and
windowed clustering.

- Engine code: `backend/analysis/services/stages.py`
- Worker entrypoint: `backend/analysis/management/commands/run_worker.py`
- Vocabulary canonicalization: `backend/analysis/services/normalize.py`
- LLM helpers (classify / judge / region infer): `backend/analysis/services/pipeline.py`
- TeleZip client + concurrency gate: `backend/analysis/services/telezip.py`

---

## 1. Data model (the relevant parts)

| Model | Role |
|---|---|
| **AnalysisTask** | One theme. Holds *all* domain config: query, languages, classify prompt, tag categories (M2M), closed categories, geo flag, dedup window/thresholds, judge prompt, generic sides. Posts & events accumulate on the task ("Model A"). |
| **ResearchRun** | A *collect job* launched from the admin for a date range. Tracks chunking progress; not a silo — it just spawns chunks. |
| **CollectChunk** | One resumable unit of collection: `(task, date_from, date_to, status, attempts, locked_at)`. A big period is split into these. |
| **Post** | One Telegram message, unique per `(task, url)`. Carries the pipeline `stage`, `stage_locked_at`, `stage_attempts`, `dedup_group`, `channel`, `classification` JSON, `is_relevant`, and the final `event` FK. |
| **Channel** | Cached channel metadata (subscribers → reach, `is_channel` vs chat, `inferred_region`). |
| **Event** | A deduplicated real-world incident: date, region/subject/settlement, summary, tags (M2M), `post_count`, `reach`, `is_corroborated`. |
| **TagCategory** | Shared registry: `key`, `label`, `closed`, `hint`, `order`. A task selects which categories to collect. |
| **Tag / TagAlias / Region / RegionAlias** | Shared, canonical vocabularies + their raw→canonical alias maps (so synonyms/cross-language/gender variants collapse to one term). |

`Post.stage` is the heart of the machine:

```
collected → enriched → preclustered → classified → deduped → done
                                                          ↘ failed
```

Out-of-scope posts (e.g. chats when the task is posts-only) are short-circuited straight
to `done` so they never clog the pipeline.

---

## 2. The five worker processes

Each stage is an independent OS process (a Docker service) running
`manage.py run_worker --stage <name>`. They share the web image and all poll the same
DB. The loop is simple and crash-tolerant:

```
for task in active_tasks:
    while STAGE_RUNNERS[stage](task):   # drain this task's queue
        ...
if nothing happened anywhere: sleep(interval)   # default 10s
```

`STAGE_RUNNERS` (`stages.py`): `collect → collect_once`, `enrich → enrich_once`,
`precluster → precluster_once`, `classify → classify_once`, `dedup → dedup_once`.

A worker exception is caught and logged — the worker stays alive and retries on the next
pass. Stages are decoupled: each only reads/writes its own stage transition, so they run
fully in parallel with no shared locks beyond per-row claims.

### Claim-based queue (no external broker)

Work is claimed atomically in Postgres with `select_for_update(skip_locked=True)`:

- `_claim_posts(task, stage, limit)` grabs up to `limit` posts at `stage` whose
  `stage_locked_at` is NULL **or** older than `LOCK_TIMEOUT` (20 min — stale-claim
  reclaim after a crash), stamps `stage_locked_at = now()`, returns their ids.
- `_claim_chunk(task)` does the same for one `CollectChunk` (pending, or running with a
  stale `locked_at`).

Because claims are row-level and skip already-locked rows, you can run **multiple
replicas of any stage** (`make scale-workers CLASSIFY=2`) and they won't double-process.

---

## 3. Stage-by-stage

### collect — `collect_once`
Claims one `CollectChunk`, calls TeleZip `/Find` for that date range (all reposts unless
`telezip_unique`), and upserts each message as a `Post` at stage `collected`
(`update_or_create` on `(task, url)` → idempotent re-runs). The TeleZip `channel_id` is
stashed in `Post.classification["_tz_channel_id"]` for the enrich step, and channel
metadata for the chunk's channels is cached.

**Adaptive split on failure:** TeleZip caps requests at ~2 min; a multi-day chunk that
errors is split into 1-day chunks (`_split_chunk`) and retried; a 1-day chunk retries up
to 4 attempts, then is marked `failed` (terminal). The collector is the *only* TeleZip
user — enrich/classify never hit it.

### enrich — `enrich_once`
**No TeleZip.** Links each post to its cached `Channel` (by the stashed channel id) and,
**only if `task.geo_enabled`**, asks the LLM for a coarse channel region hint (cached on
the channel, used later only as a geo fallback). Advances to `enriched`.

### precluster — `precluster_once` (windowed, no AI)
Cheaply collapses near-identical reposts into a `dedup_group` before any LLM cost:

1. **Scope.** Posts-only tasks keep only confirmed channels (`is_channel=True`); chats /
   unlinked posts (no reach) are finalized straight to `done` (the *antiscope*) so they
   never hold the watermark back. Comments-only is the inverse; both/neither = no filter.
2. **Window.** Only finalize days that are *settled* — `posted_at ≤ ready − dedup_window`
   (see §4) — so cross-day duplicates can still merge.
3. **Cluster.** Union-find over: identical `content_hash`, and fuzzy text similarity
   ≥ `dedup_pre_thresh` (default 82%) within the window. A **back-buffer** of already
   preclustered neighbours within the window is included as anchors so a new post merges
   into an existing group across a day boundary. Only the new posts get written/advanced.

### classify — `classify_once` (LLM)
Claims preclustered posts and classifies **one representative per `dedup_group`**
(earliest member) — so the LLM is paid once per cluster, not per repost. The prompt is
`build_classify_prompt(task)` (§5). For each rep the LLM returns `is_relevant`, optional
`region`/`settlement` (if geo), per-category `tags` lists, and a one-sentence `summary`.
The result is propagated to every member of the group; `is_relevant` is read from the
task's `relevance_field`. Advances to `classified`.

### dedup — `dedup_once` (LLM judge, windowed)
Turns relevant clusters into `Event`s, merging reports of the *same* incident across
channels and days:

1. Take relevant classified clusters in the settled window; non-relevant ones are
   finalized to `done`.
2. **Anchors:** recent `Event`s within the window are candidates to merge new clusters
   into (continuous accumulation — new posts attach to an existing event).
3. **Candidate pairs** between clusters within the time window are formed by signals:
   - `summary` fuzzy ≥ 90% **and** a shared concrete side → **forced merge** (overrides
     occasional judge mistakes, e.g. two reports of one court case);
   - text/summary fuzzy ≥ `dedup_cand_thresh` → judge;
   - shared concrete side + softer summary similarity → judge;
   - **same region** + moderate similarity → judge.
   A *shared side* uses `task.generic_sides`: umbrella terms (e.g. мігрант/місцевий) do
   **not** count as a shared signal, only concrete ones (a specific nationality) do.
4. **Judge.** Candidate pairs go to the LLM judge (`task.dedup_judge_prompt`) which sees
   the **original post text** (`_judge_text`), not the possibly-cross-contaminated
   summary — "one event or different?".
5. **Union-find** over forced + judged-yes pairs. Each resulting cluster either attaches
   its new posts to an anchor event or materializes a brand-new `Event` (`_create_event`)
   with geolocation (§6), canonical tags, reach and corroboration recomputed.

---

## 4. The watermark (why the stream stays correct)

The five stages run at different speeds, yet windowed clustering needs a day's
**neighbours** before it can finalize that day. The watermark coordinates this without
ever globally pausing:

- `_collection_frontier(task)` = earliest date still **pending/running** in collection.
  `failed` chunks are terminal and deliberately **don't** block (otherwise one
  permanently dead day would freeze the whole task forever).
- `_stage_frontier(task, stages)` = earliest `posted_at` of any post still sitting at an
  upstream stage (work not yet done).
- `_ready_through(task, pending_stages)` = `min(frontiers) − 1 day` — the latest day `D`
  such that everything `≤ D` is collected **and** past the given upstream stages.
- Each windowed stage finalizes only `posted_at ≤ ready − dedup_window`. So a day is
  touched only once its window of later neighbours has caught up — cross-day merges are
  preserved while the frontier keeps sliding forward as collection completes.

`ready = None` ("nothing blocks") means everything is collected and processed, so the
tail of the period gets finalized.

---

## 5. The classification prompt (`build_classify_prompt`)

Assembled per task at run time = **task domain rules + auto-generated JSON schema**:

- The JSON schema lists one array element per input message: `i`, `is_relevant`,
  geo fields (only if `geo_enabled`), a `tags` object with one **list field per chosen
  TagCategory**, and a `summary`.
- Per-category guidance:
  - **closed** category → "pick EXACTLY from this seed list `[...]`; if none fits, skip
    (don't invent)". The seed list is the existing `Tag`s of that category.
  - **open** category → the category's `hint` (or "free values, generalized").
- Geo note clarifies `region` = subject without the city, `settlement` = the city.

So the *same code* produces a taxicab prompt or an ethnic-clash prompt purely from the
task's selected categories — adding a category to the task changes the schema.

---

## 6. Canonicalization (`normalize.py`) — shared across tasks

Vocabularies are global so every task improves them.

- **`resolve_in_category(raw, category, closed)`** — collapse a raw tag value to one
  canonical `Tag`:
  - alias hit (cached, free) → return it;
  - **closed**: match to a seeded tag by exact / common-prefix ≥ 4 / fuzzy ≥ 85, else
    **drop** (never invent in a closed category — this is what stops "росіянка" becoming
    a new nationality);
  - **open**: one LLM call canonicalizes against existing tags (merging
    русский/росіянин/росіянка → росіянин) or creates a normalized new lowercase tag; the
    raw→canonical mapping is cached as an alias.
- **`resolve_region(raw)`** — free text → `(RF subject, settlement)`. The LLM picks a
  subject strictly from the seeded list and geolocates bare city names. A
  `CITY_SUBJECT` override table authoritatively fixes the most-confused cases (federal
  cities are their own subjects: СПб ≠ Ленінградська обл., Іркутськ ≠ Забайкалля). Long
  free text (>200 chars) isn't cached (alias column limit).
- Placeholder sanitizer turns "порожньо / невідомо / n/a / —" into an empty value.

---

## 7. TeleZip concurrency (`telezip.py`)

The TeleZip API allows only a couple of simultaneous connections. The cap lives **inside
the client**, not in the workers: `_gate()` returns a per-event-loop
`asyncio.Semaphore(settings.TELEZIP_MAX_CONCURRENCY)` (default **2**, from
`TELEZIP_MAX_CONCURRENCY` env), held via a `WeakKeyDictionary` keyed by the running loop.
Every `_request` wraps its entire retry loop in `async with _gate():`, so no matter how
many collect workers or coroutines exist, at most N TeleZip requests are in flight and
the rest queue. The 2-minute per-request limit is handled by the adaptive chunk split in
`collect_once`.

---

## 8. Failure & resume semantics

| Failure | Behaviour |
|---|---|
| Worker process crash mid-claim | `stage_locked_at` / `locked_at` goes stale; after `LOCK_TIMEOUT` (20 min) the row is reclaimable by any worker. |
| TeleZip outage / reset | Chunk splits (multi-day) or retries up to 4× then `failed`; `failed` is terminal and does **not** block the watermark. |
| Post-level error | `stage_attempts` increments; persistent failures land at `failed`. |
| Re-enqueue same period | `enqueue_collection` skips ranges already covered by done/pending chunks; `Post` upsert is idempotent on `(task, url)`. |
| Restart anything | Nothing replays from zero — every stage resumes from the DB state. |

This is why a year-long run can be launched once from the admin, survive restarts and
provider hiccups, and keep producing events as a continuous stream.
