# Comments / criticism-monitor pipeline

**Single source of truth** for how monitor (criticism-of-authority) Posts are turned into
tagged, validated `is_relevant` comments and a `% criticism` metric.

This is the **monitor / comments** pipeline — distinct from the **events** pipeline
(`docs/ARCHITECTURE.md`, the 5 stage-workers that dedup incidents into `Event`s).
Comments are **NOT** deduped: each comment is one person's opinion; the metric is *how many
people criticise*, so merging would destroy the count. The only legitimate cleanup is
collapsing exact same-author copies (`content_hash` / `also_in_chats`).

> ⚠️ Do **not** reinvent prompts or stages. The prompts are recorded in
> `backend/analysis/pilot/prompts.py` (the file's own docstring: *"Single source of truth
> for what we ask the model"*). Use the existing management commands.

---

## The flow

```
mon_collect → mon_filter → mon_prescreen (OpenRouter, cheap) → [AGENT: tag+validate, ONE pass] → Event 1:1 → done
```

### Єдина структура моделей (рішення 2026-07-05)

**Кожен відфільтрований (is_relevant) коментар матеріалізується як Event** — 1 коментар =
1 подія, БЕЗ дедупу (коментар = думка окремої людини; лічильник «скільки людей» священний).
Event: `event_date` = дата коментаря, `region_subject` = з поста (денормалізовано з каналу),
`tags` = дзеркало тегів поста, `summary` = перші 300 символів, `post_count=1`,
`reach` = підписники каналу, `review_status=approved` (валідація вже пройдена агентом).
**Усі графіки/матриця будуються ЛИШЕ по Event** — обидві природи (інциденти й критика)
живуть в одній моделі, розрізняються `task.pipeline`.

Механіка: `analysis.services.monitor_stages.sync_comment_event(post)` —
створює/оновлює/знімає подію за фактичним `is_relevant`; викликається з
`monitor_ingest_tags` (після вердикту агента) і `monitor_validate --ingest`
(downgrade → подія видаляється, `Post.event` → NULL через SET_NULL).
Разова матеріалізація історії: `_dir/materialize_comment_events.py`
(ідемпотентний — бере лише пости без події).

| # | Stage | Tool | Prompt | Effect |
|---|-------|------|--------|--------|
| 1 | collect | `monitor_collect` / `_dir/process_month_daily.py` | — | TeleZip → `Post` @ `mon_collected` |
| 2 | filter | `monitor_filter` / `mon_filter_once` | — | noise → `is_filtered=True`,`done`; kept → `mon_filtered` |
| 3 | **prescreen** | `monitor_prescreen_api` (OpenRouter) | `PRESCREEN_SYSTEM_PROMPT` | cheap yes/no on **ALL** kept posts → `classification._prescreen.could_be_criticism` |
| 4 | **tag+validate (ONE agent pass)** | `monitor_prepare_batches --require-prescreen` → Claude-Code agents → `monitor_ingest_tags` | `TAGGER_SYSTEM_PROMPT` | tags `criticism_target`/`topic`/`opinion` **and** decides criticism → `is_relevant` |

### Why steps 2+3 of the *old* design are merged (decision 2026-06-19)

Originally: **weak tagger (OpenRouter `mon_tag`) → strict agent validator** (`monitor_validate`).
That dance existed only because the weak Gemini-Flash tagger produced **22–35 % false
positives**. We **removed `mon_tag`** and replaced the two steps with **ONE strong-agent
pass**: a capable agent (Claude Haiku) tags `criticism_target + topic + opinion` AND
decides `is_criticism` in a single read. The strict prohibitions that used to live in the
validator are already encoded in `TAGGER_SYSTEM_PROMPT`; a strong model executes them in
one go. `monitor_validate` / `VALIDATOR_SYSTEM_PROMPT` are **legacy** — kept for reference,
not used in the merged flow.

`mon_prescreen` (cheap OpenRouter yes/no) is **kept** — it cuts volume ~5–10× before the
(more expensive) agent pass. The keyword-regex pre-filter used in the unattended 2025
overnight run (`_dir/OVERNIGHT.md`) was a **reliability shortcut, not the canonical design**
— it under-recalls (misses criticism without keywords, e.g. "знову підняли ціни на проїзд").

---

## Step 1 — Prescreen (OpenRouter, cheap)

```bash
python manage.py monitor_prescreen_api --task <slug> \
    --date-from 2026-03-01 --date-to 2026-05-31
# defaults: --model google/gemini-2.5-flash  --compact  --batch-size 50  --concurrency 12
```
- Scope: `Post(task)` excluding `text=""` and `classification.is_filtered=True`; skips posts
  that already have `_prescreen` (idempotent — safe to re-run / resume after a kill).
- `--compact` (default): model returns only positive indices (`{"positive":[{"i","c"}]}`),
  ~40× less output and **higher recall** (85 % vs 46 % on the 01-01 test).
- Writes `classification._prescreen = {could_be_criticism, confidence, _model, _mode}`.
- ~4–10 % come back positive. Variant `monitor_prescreen` does the same via file-based
  agents instead of the API (no per-call cost, but burns the Max-plan 5h token window).

## Step 2 — One-pass tag+validate (agents)

```bash
# a) prepare batches of prescreen-POSITIVES only
python manage.py monitor_prepare_batches --task <slug> --require-prescreen \
    --date-from 2026-03-01 --date-to 2026-05-31 \
    --out-dir <DIR> --batch-size 50
#    → writes batch_NNN.json (items:[{id,chat,region,text}]) + SYSTEM_PROMPT.md (TAGGER_SYSTEM_PROMPT)

# b) ORCHESTRATOR (Claude Code): one Haiku agent per batch, system = TAGGER_SYSTEM_PROMPT,
#    user = build_user_prompt(items). Map the positional reply back to ids and WRITE:
#    batch_NNN_done.json = {"meta":{"batch_id":N},
#      "items":[{"id":<int>,"verdict":{"criticism_target":[...],"topic":[...],
#                "opinion":[...],"proposed_tags":[...],"confidence":0.9}}]}

# c) ingest
python manage.py monitor_ingest_tags --task <slug> --done-dir <DIR>
```
- `monitor_ingest_tags` sets **`is_relevant = (criticism_target non-empty)`** — this is the
  merged "is_criticism" decision. Tags are `get_or_create`d; raw verdict stored in
  `classification.opinion`. `proposed_tags` (new criticism_target names outside the closed
  list) are created too and cleaned up later via `monitor_review_tags`.
- `--require-prescreen` filters to `_prescreen.could_be_criticism=True`; `--only-untagged`
  (default) skips already-tagged posts → resumable.

---

## The closed tag taxonomy (3 categories)

Seeded by `seed_opinion_tags.py`; full lists + examples live in `TAGGER_SYSTEM_PROMPT`.
- **criticism_target** — who is criticised (closed: крит_путін, крит_кремль, крит_МО, …,
  крит_глави_регіону, крит_мера, крит_релігійних_авторитетів). Empty ⇒ not criticism.
- **topic** — тема_СВО / мобілізації / економіки / корупції / репресій / інфраструктури /
  релігії / етнічна.
- **opinion** — підтримка_влади / нейтрально_новина / сарказм / пропаганда / теорія_змови /
  прогноз_влади. NB: `is_criticism=True` + opinion∈{нейтрально_новина, підтримка_влади,
  пропаганда} is a **contradiction** ⇒ usually a false positive (audited: ~34 %).

The tagger periodically grows `criticism_target` (open via `proposed_tags`) — dedupe with
`monitor_review_tags` (`REVIEW_TAGS_PROMPT`).

---

## The "% criticism" denominator

The numerator = relevant comments (`Post.is_relevant=True`). The denominator = **all
messages**, kept in `ChannelDailyStat.telezip_total` (authoritative count from TeleZip
`unique=False`, per `(task, channel, day)`). It is materialised so raw non-relevant Posts
can be pruned without losing the denominator. Charts use
`COALESCE(telezip_total, total)`. Refresh it with `_dir/recount_daily.py`
(`UNIQUE=0`, per-day to avoid whole-month TeleZip timeouts).

- **region** of a comment = `channel.region_subject` (curated FK; carries population for
  per-100k). `settlement` = open tag.

---

## Operational notes

- **Collect / recount per-DAY**, not per-month: a whole-month `find_posts_range` on a
  mega-chat wastes up to 180 s per attempt on TeleZip 500/timeout before splitting. Use
  `_dir/process_month_daily.py` / `_dir/recount_daily.py`. Idempotent (URL dedup).
- **TeleZip "Temporary failure in name resolution"** = the user's VPN DNS resolver is down,
  not Docker. TeleZip is reachable by IP without the VPN — add a static container hosts
  entry: `echo "77.88.192.66 api.telezip.net" >> /etc/hosts` (keep 127.0.0.11 for `db`).
  Re-applied per run by `_dir/collect_supervisor.sh`.
- **Idempotency / resume**: every stage skips already-done rows, so any job can be killed
  and re-run. Workers reload code only on `docker compose restart worker-…`.
- **Workers vs orchestration**: prescreen can run as a docker worker (`mon_prescreen_once`,
  also OpenRouter) or one-shot (`monitor_prescreen_api`). The agent tag+validate pass is
  **orchestrated in rounds by Claude Code**, not a docker worker.

## Prompts & commands (where everything lives)

- Prompts: `backend/analysis/pilot/prompts.py` — `PRESCREEN_SYSTEM_PROMPT`,
  `TAGGER_SYSTEM_PROMPT`, `VALIDATOR_SYSTEM_PROMPT` (legacy), `REVIEW_TAGS_PROMPT`,
  `build_user_prompt()`.
- Commands: `backend/analysis/management/commands/monitor_*.py`
  (`monitor_collect`, `monitor_filter`, `monitor_prescreen[_api]`, `monitor_prepare_batches`,
  `monitor_ingest_tags`, `monitor_validate` (legacy), `monitor_review_tags`).
- Stage engine: `backend/analysis/services/monitor_stages.py`.
- Re-collection / denominator scripts: `backend/_dir/*_daily.py`, `collect_supervisor.sh`
  (host, gitignored).
