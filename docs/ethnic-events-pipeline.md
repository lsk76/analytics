# Ethnic-tension EVENT-detection pipeline (per republic)

Validated on the **Dagestan / C2-C3-C4** pilot. Detects **events reported by channels**
(«повідомлення про подію») — distinct from the comments-criticism pipeline
(`comments-analysis-pipeline.md`, which measures what users *discuss*). Output = `Event`
rows tagged by event-type, each linked to its source publications, then audited.

Run it **per (republic × event-types × period)**. Below is the end-to-end scheme +
the hard-won rules that make it actually work.

---

## The flow (8 stages, one pass)

```
0 setup tags+task → 1 pick channels → 2 collect (TeleZip, unique, /month)
→ 3 keyword AND-filter → 4 LLM classify (type + geo) → 5 dedup into incidents
→ 6 write Events + linked Posts → 7 audit each post (is_relevant) → 8 count valid events
```

| # | Stage | Tool | Output |
|---|-------|------|--------|
| 0 | tags + events-task | one-shot: `TagCategory("ethnic_event")` + tags; clone an `events` task (e.g. from task=1) | task id, tag ids |
| 1 | channels | SQL (see rule below) | top-N **local** channel ids/tg_ids |
| 2 | collect | `_dir/collect_ethnic_<rep>.py` (TeleZip `find_posts_range('*', unique=True)` per channel **per month**) | candidates JSON `{ch,subs,date,url,t,kw_types}` |
| 3 | keyword AND-filter | inside the collect script | only topic∧action matches |
| 4 | classify | Workflow, Sonnet, batched | confirmed `{i, types, summary}` (geo=republic) |
| 5 | dedup → incidents | LLM-cluster (preferred) or keyword group | groups of reports |
| 6 | write events | `_dir/create_ethnic_events_grouped.py` | `Event` + linked `Post` rows (url, channel) → fills admin **«Усі публікації»** |
| 7 | audit | Workflow, Sonnet, one verdict/post on **original text** → `_dir/update_ethnic_audit.py` | `Post.is_relevant` + `classification._audit`; cascade `Event.review_status` (approved if ≥1 valid post, else rejected) |
| 8 | count | SQL | valid (`approved`) events per type per period for the table |

---

## The rules that make it work (lessons from the pilot)

1. **Channels = LOCAL event-producers, NOT biggest-subscriber.** Top-5 by subscribers are
   general aggregators (e.g. `Cbpub` 613k posted Iran/Ukraine/federal news → **0 events**).
   Rank by how many events a channel already produced (join `analysis_event` of the
   ethnic-clash task=1) — e.g. `ekho_dagestana1`, `monitoring_05` (362 subs!), `chpdagestan_05`.
   ```sql
   SELECT c.username, count(DISTINCT e.id) ev FROM analysis_event e
     JOIN analysis_post p ON p.event_id=e.id JOIN analysis_channel c ON c.id=p.channel_id
     WHERE e.task_id=1 AND e.region_subject_id=<REG> GROUP BY 1 ORDER BY 2 DESC;
   ```
   **Exclude ultra-high-volume specialist channels** (e.g. `extremistus`: 8933 candidates,
   reaps the collector) — collect those separately, per-day, if needed.

2. **Keywords = AND-logic (topic ∧ action), not OR.** Topic-only floods with noise
   (any «мечеть/шаріат/намаз» post matched → 11 404 candidates, mostly religious content).
   Require **both** a topic term **and** an event/action term — exactly how task=1 works:
   `(этнос-терміни) +(драка/избил/...)`. AND-logic cut 11 404 → 367.

3. **Classifier MUST require geo = the republic.** Keyword noise is full of Iran/Istanbul/
   Ukraine. Prompt: «подія СТАЛАСЯ В <республіці> (конкретне місто/район)». This alone
   moved the pilot from 0 real events (wrong channels+geo noise) to 39.

4. **C4 trap — state «unity» propaganda ≠ diaspora activity.** Mainstream channels report
   «Год единства народов», «День России», «Хоровод дружбы», forums «Наша Россия — наш
   Дагестан» — top-down state PR, the OPPOSITE of the column's intent (diaspora statements /
   grassroots ethnic-support). The audit correctly rejected **all 10** C4 candidates.
   Real C4 ≈ 0 in mainstream (consider a separate «держ-пропаганда єдності» tag — it IS a
   signal, just a different one).

5. **C2 (protests vs discrimination) ≈ 0 in mainstream** — suppressed/unreported. Expect 0;
   don't force it.

6. **TeleZip `messageUrl` is UNRELIABLE for some channels.** Live `t.me/<ch>/<id>` may point
   to a *different* post than TeleZip's content (e.g. `chpdagestan_05/15630` → 2024 traffic
   post, but TeleZip content = 2026 Губден). So **audit on the captured TEXT** (= the
   original content), NOT by re-opening the URL. To READ a Telegram post that does resolve:
   `t.me/<ch>/<id>?embed=1` (plain URL returns only the embed widget). **TODO: fix URL
   generation** before trusting «Усі публікації» links.

7. **Dedup is essential** — one incident is reported by many channels (Уллубіяул = 9 posts,
   Губден = 8). Group reports → 1 Event with `post_count` = N source posts. Keyword-grouping
   is rough (mis-merges «Махачкала-Дербент» ж/д with «Дербент» verdict, mis-dates) →
   **prefer an LLM incident-clustering step**.

8. **Audit verdict lives on the Post**: `Post.is_relevant` (True/valid, False/invalid) +
   reason in `classification._audit`; the Event's `review_status` cascades.

9. **Ops**: collect per-month per-channel with `unique=True`; re-apply the telezip
   `/etc/hosts` fix (VPN-DNS down) before each run — see `vpn-dns-hosts-fix`.

---

## Pilot result (Dagestan 2026, after audit)

| type | valid events 2026 / Apr-May |
|---|---|
| C2 протести проти етнодискримінації | 0 / 0 |
| C3 радикальні/націоналістичні рухи | 6 / 1 |
| C4 діаспори/форуми | 0 / 0 (all = state propaganda) |

27 valid posts / 12 invalid. Events in **task=6** (`ethnic-tension-events`), tag category
`ethnic_event` (`протест_проти_етнодискримінації`, `активізація_націоналістичного_руху`,
`діаспора_культурний_форум`).

## To reuse for another republic
1. Channel SQL (rule 1) → top-N local event channels for that `region_subject_id`.
2. Copy `_dir/collect_ethnic_dag.py` → set `CH_IDS`; keep the AND-keyword sets (tune per
   republic's specifics, e.g. language names).
3. Run collect → classify Workflow (geo = that republic) → group → create events → audit.
4. Count `approved` events per type per period.

## Open TODO (before scaling)
- Fix TeleZip→t.me URL generation (rule 6).
- Replace keyword incident-grouping with LLM clustering (rule 7).
- Add a distinct «держ-пропаганда єдності» tag (rule 4) — capture, don't discard.
- Wrap stages 1-8 into one parameterized driver `(region_id, types, period)`.
