"""
Configurable analysis pipeline. Driven entirely by AnalysisTask config.

Stages (each updates ResearchRun.status):
  collect       — TeleZip /Find (unique, languages), monthly chunks  -> Post rows
  enrich        — channel meta (is_channel, title/about) + LLM region inference -> Channel
  classify      — LLM batches per task.classify_system_prompt        -> Post.classification
  dedup         — fuzzy pre-merge + pair-LLM judging                 -> Event (+normalized sides/type)
  aggregate     — breakdowns by month/region/type/sides             -> ResearchRun.stats

Classification output contract (LLM must return a JSON array, one object per input line):
  { "<relevance_field>": bool, "region": str, "sides": [str,...], "type": str, "summary": str }
"""
import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.utils import timezone as djtz
from rapidfuzz.fuzz import token_set_ratio

from analysis.models import Post, Event, Channel, ResearchRun
from .telezip import TelezipClient
from . import llm
from .normalize import resolve_nationality, resolve_conflict_type, resolve_region

logger = logging.getLogger(__name__)

CLASSIFY_BATCH = 15
CONCURRENCY = 20


# --------------------------------------------------------------------------- helpers

def _post_dt(s):
    """Parse TeleZip date -> UTC. Naive timestamps are TeleZip-local (UTC+2)."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone(timedelta(hours=2)))
    return d.astimezone(timezone.utc)


def _norm(text):
    import re
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(w for w in text.split() if len(w) > 3)


def _day_chunks(dfrom, dto):
    """Collect day-by-day: /Find returns ALL matches at once, so a wide window
    times out. One day of the ethnic query is ~400 posts / ~45s — reliable."""
    cur = dfrom
    one = timedelta(days=1)
    while cur < dto:
        nxt = min(cur + one, dto)
        yield cur, nxt
        cur = nxt


def _set_status(run, status):
    run.status = status
    run.save(update_fields=["status"])


# --------------------------------------------------------------------------- collect

async def _collect_async(task, dfrom, dto):
    out = []
    async with TelezipClient(settings.TELEZIP_API_KEY, settings.TELEZIP_BASE_URL) as tz:
        for a, b in _day_chunks(dfrom, dto):
            posts = await tz.find_posts(task.telezip_query, a, b, task.languages or None,
                                        unique=task.telezip_unique)
            out.extend(posts)
            logger.info("collect %s..%s: +%d", a.date(), b.date(), len(posts))
    return out


def collect(run):
    _set_status(run, "collecting")
    task = run.task
    dfrom = datetime.combine(run.date_from, datetime.min.time(), tzinfo=timezone.utc)
    dto = datetime.combine(run.date_to, datetime.max.time(), tzinfo=timezone.utc)
    rows = asyncio.run(_collect_async(task, dfrom, dto))

    n = 0
    for p in rows:
        url = p.get("message_url")
        if not url:
            continue
        post, created = Post.objects.update_or_create(
            task=task, url=url,
            defaults={
                "run": run,
                "channel_name": p.get("channel_name") or "",
                "telezip_date": _post_dt(p.get("date")),
                "posted_at": _post_dt(p.get("date")),
                "text": p.get("content") or "",
                "content_hash": p.get("content_hash") or "",
                "telezip_mid": p.get("mid"),
            },
        )
        # stash telezip channel id on the post's classification meta for enrichment
        if p.get("channel_id"):
            post.classification = {**(post.classification or {}), "_tz_channel_id": p["channel_id"]}
            post.save(update_fields=["classification"])
        n += 1
    run.posts_collected = Post.objects.filter(run=run).count()
    run.save(update_fields=["posts_collected"])
    logger.info("collected %d posts", n)


# --------------------------------------------------------------------------- enrich (channels + region)

REGION_SYS = (
    "По назві та опису Telegram-каналу визнач, до якого СУБ'ЄКТА РФ він прив'язаний "
    "(область/республіка/край/місто), якщо це регіональний канал. Якщо канал "
    "загальноросійський/тематичний/незрозумілий — поверни порожній рядок. "
    "Відповідай лише назвою регіону або порожнім рядком, без пояснень."
)


async def _infer_regions(channels):
    """channels: list of (cid, title, about) -> {cid: region}."""
    sem = asyncio.Semaphore(CONCURRENCY)
    out = {}

    async def one(cid, title, about):
        async with sem:
            txt = f"Назва: {title}\nОпис: {about[:400]}"
            r = await llm.query([{"role": "system", "content": REGION_SYS},
                                 {"role": "user", "content": txt}])
            out[cid] = (r or "").strip().strip('"')[:128]

    await asyncio.gather(*[one(c, t, a) for c, t, a in channels])
    return out


CHANNEL_CONCURRENCY = 2   # TeleZip allows 2 parallel requests


def enrich(run):
    _set_status(run, "enriching")
    posts = list(Post.objects.filter(run=run))
    cid_by_post = {p.id: (p.classification or {}).get("_tz_channel_id") for p in posts}
    unique_cids = {c for c in cid_by_post.values() if c}

    # reuse already-cached channels — only fetch the missing ones
    chan_by_cid = {c.tg_id: c for c in Channel.objects.filter(tg_id__in=unique_cids)}
    missing = [cid for cid in unique_cids if cid not in chan_by_cid]
    logger.info("enrich: %d channels (%d cached, %d to fetch)",
                len(unique_cids), len(chan_by_cid), len(missing))

    # fetch missing channel meta — max 2 parallel (TeleZip limit)
    async def fetch_meta(cids):
        res = {}
        sem = asyncio.Semaphore(CHANNEL_CONCURRENCY)
        async with TelezipClient(settings.TELEZIP_API_KEY, settings.TELEZIP_BASE_URL) as tz:
            async def one(cid):
                async with sem:
                    res[cid] = await tz.get_channel(cid)
            await asyncio.gather(*[one(c) for c in cids])
        return res

    meta = asyncio.run(fetch_meta(missing)) if missing else {}
    for cid, m in meta.items():
        if not m:
            continue
        ch, _ = Channel.objects.update_or_create(
            tg_id=m["tg_id"] or cid,
            defaults={
                "username": m["username"], "title": m["title"], "description": m["about"],
                "subscribers": m["subscribers"] or 0, "language": m["language"],
                "is_channel": m["is_channel"], "enriched": True, "fetched_at": djtz.now(),
            },
        )
        chan_by_cid[cid] = ch

    # region inference only for channels still lacking it (cached on Channel)
    to_infer = [(cid, ch.title, ch.description) for cid, ch in chan_by_cid.items()
                if not ch.inferred_region and (ch.title or ch.description)]
    if to_infer:
        regions = asyncio.run(_infer_regions(to_infer))
        for cid, region in regions.items():
            ch = chan_by_cid.get(cid)
            if ch and region:
                ch.inferred_region = region
                ch.save(update_fields=["inferred_region"])

    # link posts -> channel (bulk)
    changed = []
    for p in posts:
        ch = chan_by_cid.get(cid_by_post.get(p.id))
        if ch and p.channel_id != ch.id:
            p.channel = ch
            changed.append(p)
    if changed:
        Post.objects.bulk_update(changed, ["channel"], batch_size=500)


# --------------------------------------------------------------------------- classify

async def _classify_batches(system_prompt, batches, model):
    sem = asyncio.Semaphore(CONCURRENCY)
    results = {}

    async def one(bi, texts):
        async with sem:
            user = "\n".join(f"[{i}] {t[:700]}" for i, t in enumerate(texts))
            raw = await llm.query(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}],
                model=model)
            results[bi] = llm.extract_json(raw) or []

    await asyncio.gather(*[one(bi, texts) for bi, texts in enumerate(batches)])
    return results


def precluster(run):
    """
    Cheap grouping BEFORE the AI classifier (no LLM):
      stage 0 — exact reposts by content_hash
      stage 1 — near-identical reposts by fuzzy text, within the dedup window
    Writes Post.dedup_group (root post id) so the AI runs once per group, not per repost.
    """
    _set_status(run, "classifying")
    task = run.task
    qs = Post.objects.filter(run=run)
    if task.channels_only:
        qs = qs.exclude(channel__is_channel=False)   # drop chats early
    posts = [p for p in qs.order_by("posted_at") if p.posted_at]
    n = len(posts)
    if n == 0:
        return

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # stage 0: exact duplicates by content hash (whole run, no window)
    by_hash = defaultdict(list)
    for i, p in enumerate(posts):
        if p.content_hash:
            by_hash[p.content_hash].append(i)
    for g in by_hash.values():
        for k in g[1:]:
            union(g[0], k)

    # stage 1: near-identical reposts by fuzzy TEXT (no summary yet), windowed
    norms = [_norm(p.text) for p in posts]
    win = timedelta(days=task.dedup_window_days)
    for i in range(n):
        for j in range(i + 1, n):
            if posts[j].posted_at - posts[i].posted_at > win:
                break
            if token_set_ratio(norms[i], norms[j]) >= task.dedup_pre_thresh:
                union(i, j)

    for i in range(n):
        posts[i].dedup_group = posts[find(i)].id
    Post.objects.bulk_update(posts, ["dedup_group"], batch_size=500)
    logger.info("precluster: %d posts -> %d groups", n, len({find(k) for k in range(n)}))


def classify(run):
    """AI classification — ONE representative per precluster group, then propagated."""
    _set_status(run, "classifying")
    task = run.task
    posts = list(Post.objects.filter(run=run, dedup_group__isnull=False, is_classified=False))
    if not posts:
        return

    groups = defaultdict(list)
    for p in posts:
        groups[p.dedup_group].append(p)
    reps = [members[0] for members in groups.values()]
    logger.info("classify: %d groups (from %d posts) -> LLM on reps only", len(reps), len(posts))

    batches = [reps[i:i + CLASSIFY_BATCH] for i in range(0, len(reps), CLASSIFY_BATCH)]
    text_batches = [[p.text for p in b] for b in batches]
    model = task.llm_model or None
    results = asyncio.run(_classify_batches(task.classify_system_prompt, text_batches, model))

    rep_cls = {}
    for bi, batch in enumerate(batches):
        arr = results.get(bi) or []
        by_i = {}
        for k, obj in enumerate(arr):
            if isinstance(obj, dict):
                by_i[obj.get("i", k)] = obj
        for j, rep in enumerate(batch):
            cls = by_i.get(j, {})
            cls.pop("i", None)
            rep_cls[rep.id] = cls

    rfield = task.relevance_field
    for members in groups.values():
        cls = rep_cls.get(members[0].id, {})
        for p in members:
            c = dict(cls)
            keep_tz = (p.classification or {}).get("_tz_channel_id")
            if keep_tz is not None:
                c["_tz_channel_id"] = keep_tz
            p.classification = c
            p.is_classified = True
            p.is_relevant = bool(c.get(rfield))
            p.save(update_fields=["classification", "is_classified", "is_relevant"])

    run.posts_relevant = Post.objects.filter(run=run, is_relevant=True).count()
    run.save(update_fields=["posts_relevant"])


# --------------------------------------------------------------------------- dedup (pair-LLM)

PAIR_SYS = (
    "Два новинні повідомлення. Це ОДНА І ТА САМА реальна подія (той самий інцидент: "
    "ті самі учасники, місце, обставини — навіть якщо слова та назва місця різні), "
    "чи РІЗНІ події? Відповідай одним словом: ОДНА або РІЗНІ."
)


async def _judge_pairs(pairs_text):
    sem = asyncio.Semaphore(CONCURRENCY)
    same = [False] * len(pairs_text)

    async def one(k, a, b):
        async with sem:
            r = await llm.query([{"role": "system", "content": PAIR_SYS},
                                 {"role": "user", "content": f"A: {a[:280]}\nB: {b[:280]}"}])
            same[k] = (r or "").strip().lower().startswith(("одна", "одно", "так", "да", "yes"))

    await asyncio.gather(*[one(k, a, b) for k, (a, b) in enumerate(pairs_text)])
    return same


def _summary_of(post):
    return (post.classification or {}).get("summary") or post.text[:200]


def dedup(run):
    """
    AI dedup over PRECLUSTERED relevant groups (one representative each):
    candidate group-pairs within the window (fuzzy text/summary) are judged by the
    LLM ("ОДНА чи РІЗНІ?") and merged. Events are written only at the end.
    """
    _set_status(run, "deduplicating")
    task = run.task
    rel = [p for p in Post.objects.filter(run=run, is_relevant=True, dedup_group__isnull=False)
           .select_related("channel") if p.posted_at]
    Event.objects.filter(run=run).delete()
    if not rel:
        run.events_total = 0
        run.events_corroborated = 0
        run.save(update_fields=["events_total", "events_corroborated"])
        return

    by_group = defaultdict(list)
    for p in rel:
        by_group[p.dedup_group].append(p)
    # order groups by their earliest post; representative = earliest post in group
    gids = sorted(by_group, key=lambda g: min(p.posted_at for p in by_group[g]))
    rep = {g: min(by_group[g], key=lambda p: p.posted_at) for g in gids}
    gdate = {g: rep[g].posted_at for g in gids}
    gtext = {g: _norm(rep[g].text) for g in gids}
    gsum = {g: _norm(_summary_of(rep[g])) for g in gids}
    win = timedelta(days=task.dedup_window_days)

    parent = {g: g for g in gids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # candidate group-pairs within window (fuzzy on text or summary)
    m = len(gids)
    cand = []
    for ai in range(m):
        for bi in range(ai + 1, m):
            ga, gb = gids[ai], gids[bi]
            if gdate[gb] - gdate[ga] > win:
                break
            if max(token_set_ratio(gtext[ga], gtext[gb]),
                   token_set_ratio(gsum[ga], gsum[gb])) >= task.dedup_cand_thresh:
                cand.append((ga, gb))
    logger.info("dedup: %d groups, %d candidate pairs -> LLM", m, len(cand))
    if cand:
        pairs_text = [(_summary_of(rep[ga]), _summary_of(rep[gb])) for ga, gb in cand]
        same = asyncio.run(_judge_pairs(pairs_text))
        for (ga, gb), s in zip(cand, same):
            if s:
                union(ga, gb)

    # final event clusters = merged groups -> all their posts
    final = defaultdict(list)
    for g in gids:
        final[find(g)].extend(by_group[g])

    for posts_in in final.values():
        posts_in.sort(key=lambda p: p.posted_at)
        head = posts_in[0]
        cls = head.classification or {}
        region = (cls.get("region") or "").strip()
        if not region and head.channel and head.channel.inferred_region:
            region = head.channel.inferred_region  # requirement #1 fallback
        region_subject, settlement = resolve_region(region) if region else (None, "")
        ev = Event.objects.create(
            task=task, run=run,
            event_date=head.posted_at.date(),
            region=region, region_subject=region_subject, settlement=settlement,
            conflict_type=resolve_conflict_type(cls.get("type") or "") if cls.get("type") else None,
            summary=cls.get("summary") or head.text[:300],
            post_count=len(posts_in),
            is_corroborated=len({p.channel_id for p in posts_in if p.channel_id}) >= 2,
        )
        side_objs = [o for raw in (cls.get("sides") or []) if (o := resolve_nationality(raw))]
        if side_objs:
            ev.sides.set(side_objs)
        chans = {}
        for p in posts_in:
            p.event = ev
            if p.channel:
                chans[p.channel_id] = p.channel.subscribers or 0
        Post.objects.bulk_update(posts_in, ["event"], batch_size=500)
        ev.reach = sum(chans.values())
        ev.save(update_fields=["reach"])

    run.events_total = Event.objects.filter(run=run).count()
    run.events_corroborated = Event.objects.filter(run=run, is_corroborated=True).count()
    run.save(update_fields=["events_total", "events_corroborated"])


# --------------------------------------------------------------------------- aggregate

def aggregate(run):
    events = (Event.objects.filter(run=run)
              .prefetch_related("sides").select_related("conflict_type", "region_subject"))
    by_month, by_region, by_type, by_side = Counter(), Counter(), Counter(), Counter()
    for e in events:
        by_month[(e.event_date.strftime("%Y-%m") if e.event_date else "?")] += 1
        by_region[e.region_subject.name if e.region_subject else "?"] += 1
        by_type[e.conflict_type.name if e.conflict_type else "?"] += 1
        for s in e.sides.all():
            by_side[s.name] += 1
    run.stats = {
        "by_month": dict(sorted(by_month.items())),
        "by_region": dict(by_region.most_common(40)),
        "by_type": dict(by_type.most_common()),
        "by_side": dict(by_side.most_common(40)),
    }
    run.status = "completed"
    run.finished_at = djtz.now()
    run.save(update_fields=["stats", "status", "finished_at"])


# --------------------------------------------------------------------------- orchestrator

def run_pipeline(run: ResearchRun):
    run.started_at = djtz.now()
    run.params = {
        "telezip_query": run.task.telezip_query,
        "languages": run.task.languages,
        "channels_only": run.task.channels_only,
        "relevance_field": run.task.relevance_field,
        "dedup_window_days": run.task.dedup_window_days,
        "dedup_pre_thresh": run.task.dedup_pre_thresh,
        "dedup_cand_thresh": run.task.dedup_cand_thresh,
        "llm_model": run.task.llm_model or settings.LLM_MODEL,
    }
    run.save(update_fields=["started_at", "params"])
    try:
        collect(run)        # TeleZip -> Post (all reposts)
        enrich(run)         # channel meta + region inference
        precluster(run)     # hash + fuzzy (window) -> Post.dedup_group, NO AI
        classify(run)       # AI on ONE rep per group, propagated
        dedup(run)          # AI on candidate group-pairs (window) -> Event at the end
        aggregate(run)
    except Exception as e:  # noqa: BLE001
        logger.exception("pipeline failed")
        run.status = "failed"
        run.error = str(e)[:2000]
        run.finished_at = djtz.now()
        run.save(update_fields=["status", "error", "finished_at"])
        raise
    return run
