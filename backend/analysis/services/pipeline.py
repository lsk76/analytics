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
from .normalize import resolve_region

logger = logging.getLogger(__name__)

CLASSIFY_BATCH = 8   # smaller batch => less cross-item summary contamination
CONCURRENCY = 40     # parallel LLM calls; dense dedup windows make THOUSANDS of judge
                     # calls per tick — 20 made a tick ~25min, 40 ~halves it. Raise if
                     # OpenRouter doesn't 429.


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


def _date_chunks(dfrom, dto, chunk_days):
    """Split [dfrom, dto) into chunks of `chunk_days` for TeleZip (~2 min/request).
    Independent of the dedup window — dedup spans the whole collected period."""
    cur = dfrom
    step = timedelta(days=max(1, chunk_days))
    while cur < dto:
        nxt = min(cur + step, dto)
        yield cur, nxt
        cur = nxt


def _set_status(run, status):
    run.status = status
    run.save(update_fields=["status"])


# --------------------------------------------------------------------------- collect

async def _collect_async(task, dfrom, dto):
    out = []
    async with TelezipClient(settings.TELEZIP_API_KEY, settings.TELEZIP_BASE_URL) as tz:
        for a, b in _date_chunks(dfrom, dto, task.collect_chunk_days):
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
    client = llm.make_client()

    async def one(cid, title, about):
        async with sem:
            txt = f"Назва: {title}\nОпис: {about[:400]}"
            # region name is a short string — 64 tokens is plenty
            r = await llm.query([{"role": "system", "content": REGION_SYS},
                                 {"role": "user", "content": txt}],
                                client=client, max_tokens=64)
            out[cid] = (r or "").strip().strip('"')[:128]

    try:
        await asyncio.gather(*[one(c, t, a) for c, t, a in channels])
    finally:
        await client.close()
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
    client = llm.make_client()

    async def one(bi, texts):
        async with sem:
            user = "\n".join(f"[{i}] {t[:700]}" for i, t in enumerate(texts))
            # JSON array of N verdicts (one per post in batch) — ~500 tokens per item
            # incl. tags + summary; cap at 4000 to fit batches of 8 comfortably.
            raw = await llm.query(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}],
                model=model, client=client, max_tokens=4000)
            results[bi] = llm.extract_json(raw) or []

    try:
        await asyncio.gather(*[one(bi, texts) for bi, texts in enumerate(batches)])
    finally:
        await client.close()
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
    # posts = channel messages (is_channel True/unknown); comments = chats (is_channel False)
    if task.search_posts and not task.search_comments:
        qs = qs.exclude(channel__is_channel=False)
    elif task.search_comments and not task.search_posts:
        qs = qs.filter(channel__is_channel=False)
    # both (or neither) -> everything
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

    for members in groups.values():
        cls = rep_cls.get(members[0].id, {})
        for p in members:
            c = dict(cls)
            keep_tz = (p.classification or {}).get("_tz_channel_id")
            if keep_tz is not None:
                c["_tz_channel_id"] = keep_tz
            p.classification = c
            p.is_classified = True
            p.is_relevant = bool(c.get("is_relevant"))
            p.save(update_fields=["classification", "is_classified", "is_relevant"])

    run.posts_relevant = Post.objects.filter(run=run, is_relevant=True).count()
    run.save(update_fields=["posts_relevant"])


# --------------------------------------------------------------------------- dedup (pair-LLM)

# Neutral, domain-agnostic fallback judge prompt (used only if a task did not set
# its own dedup_judge_prompt). Domain-specific wording lives on the task.
_DEFAULT_JUDGE = (
    "Дано два повідомлення (A і B). Чи описують вони ОДИН І ТОЙ САМИЙ конкретний "
    "інцидент: те саме місце, ті самі учасники, ті самі дії?\n"
    "Спільна тема НЕ означає одну подію. Якщо місце, учасники або суть події різні — РІЗНІ.\n"
    "Відповідай одним словом: ОДНА або РІЗНІ."
)


async def _judge_pairs(pairs_text, system=None):
    system = system or _DEFAULT_JUDGE
    sem = asyncio.Semaphore(CONCURRENCY)
    same = [False] * len(pairs_text)
    client = llm.make_client()

    async def one(k, a, b):
        async with sem:
            # dedup judge replies "ОДНА" or "РІЗНІ" — 16 tokens is more than enough
            r = await llm.query([{"role": "system", "content": system},
                                 {"role": "user", "content": f"A: {a[:280]}\nB: {b[:280]}"}],
                                client=client, max_tokens=16, timeout=12)
            same[k] = (r or "").strip().lower().startswith(("одна", "одно", "так", "да", "yes"))

    try:
        await asyncio.gather(*[one(k, a, b) for k, (a, b) in enumerate(pairs_text)])
    finally:
        await client.close()
    return same


def _summary_of(post):
    return (post.classification or {}).get("summary") or post.text[:200]


def _judge_text(post):
    """What the pair-judge sees: the real post text (truncated), with the LLM
    summary appended as a hint. Text is the source of truth; summary may be wrong."""
    text = (post.text or "").strip()
    summ = (post.classification or {}).get("summary") or ""
    out = text[:360]
    if summ and _norm(summ) not in _norm(out):
        out = f"{out}\n(стисло: {summ[:160]})"
    return out or summ


def _create_event(run, task, posts_in):
    """Materialize one finalized cluster of posts into an Event (region/sides/type/reach)."""
    posts_in = sorted(posts_in, key=lambda p: p.posted_at)
    head = posts_in[0]
    cls = head.classification or {}
    region = (cls.get("region") or "").strip()
    sett_hint = (cls.get("settlement") or "").strip()
    if not region and head.channel and head.channel.inferred_region:
        region = head.channel.inferred_region  # channel-name/desc fallback
    # feed city + subject so the resolver can geolocate the settlement too
    loc = ", ".join(x for x in (sett_hint, region) if x)
    region_subject, settlement = resolve_region(loc) if loc else (None, "")
    if not settlement and sett_hint:
        settlement = sett_hint  # keep classifier's city even if resolver missed it
    ev = Event.objects.create(
        task=task, run=run,
        event_date=head.posted_at.date(),
        region=region, region_subject=region_subject, settlement=settlement,
        summary=cls.get("summary") or head.text[:300],
        post_count=len(posts_in),
    )
    # NOTE: legacy run-scoped pipeline (superseded by services/stages.py). Tag
    # resolution removed with resolve_tag/resolve_conflict_tag; not used by workers.
    chans = {}
    for p in posts_in:
        p.event = ev
        if p.channel:
            chans[p.channel_id] = p.channel.subscribers or 0
    Post.objects.bulk_update(posts_in, ["event"], batch_size=500)
    ev.reach = sum(chans.values())
    ev.save(update_fields=["reach"])
    return ev


def _cluster_of(posts):
    """A working cluster: posts + representative (earliest) + fuzzy keys + date span."""
    posts = sorted(posts, key=lambda p: p.posted_at)
    rep = posts[0]
    cls = rep.classification or {}
    return {
        "posts": posts, "rep": rep,
        "text": _norm(rep.text), "sum": _norm(_summary_of(rep)),
        "sides": [_norm(s) for s in (cls.get("sides") or []) if s],
        "region": _norm(cls.get("region") or ""),
        "dmin": posts[0].posted_at, "dmax": posts[-1].posted_at,
    }


# Umbrella terms that are too generic to indicate the SAME event on their own.
# (almost every migrant story shares "мігрант"; almost every clash shares "русский".)
GENERIC_SIDES = {
    "мігрант", "мигрант", "мігранти", "приїжджий", "приезжий", "нелегал", "гастарбайтер",
    "місцевий", "местный", "житель", "іноземець", "иностранец", "чужинець",
    "кавказець", "кавказец", "азіат", "азиат", "діаспора", "диаспора", "етнічний",
    "росіянин", "русский", "росіянка", "українець", "українка", "слов янин",
    "охорона", "охоронець", "поліція", "полиция", "силовик", "поліцейський", "коренной",
}


def _is_generic(side, generic=None):
    return any(token_set_ratio(side, g) >= 88 for g in (generic or GENERIC_SIDES))


def _shared_side(a, b, generic=None):
    """True only if the clusters share a SPECIFIC participant group, not just a
    generic umbrella term (per-task `generic`, defaults to GENERIC_SIDES)."""
    sa = [s for s in a["sides"] if not _is_generic(s, generic)]
    sb = [s for s in b["sides"] if not _is_generic(s, generic)]
    return any(token_set_ratio(x, y) >= 80 for x in sa for y in sb)


def dedup(run):
    """
    Sliding micro-batch dedup (streaming):
      * groups are processed chronologically, one DAY at a time;
      * only 'open' events within the window stay in memory;
      * the day's candidate pairs (new vs open + new vs new) are judged by the LLM
        IN PARALLEL (micro-batch), merged via union-find;
      * events whose newest post ages out of the window are finalized and written.
    Combines batch parallelism (within a day) with streaming (across days).
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
    clusters = sorted((_cluster_of(g) for g in by_group.values()), key=lambda c: c["dmin"])

    win = timedelta(days=task.dedup_window_days)

    def fuzzy(a, b):
        return max(token_set_ratio(a["text"], b["text"]), token_set_ratio(a["sum"], b["sum"]))

    active = []          # open clusters (events still within the window)
    n_events = n_llm = 0
    i, N = 0, len(clusters)

    while i < N:
        day = clusters[i]["dmin"].date()
        day_new = []
        while i < N and clusters[i]["dmin"].date() == day:
            day_new.append(clusters[i])
            i += 1

        # finalize active clusters that can't get more reposts (aged out of window)
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        keep = []
        for c in active:
            if day_start - c["dmax"] > win:
                _create_event(run, task, c["posts"])
                n_events += 1
            else:
                keep.append(c)
        active = keep

        # candidate pairs: each NEW cluster vs (active + earlier new).
        # A pair is a candidate if the texts/summaries are similar enough, OR they
        # share a participant group with a softer summary match (same incident,
        # different wording — e.g. 3 framings of one bus attack).
        pool = active + day_new
        base = len(active)
        soft = max(35, task.dedup_cand_thresh - 20)
        pairs = []
        for b in range(base, len(pool)):       # b = a new cluster
            for a in range(0, b):              # vs everything before it
                if fuzzy(pool[a], pool[b]) >= task.dedup_cand_thresh:
                    pairs.append((a, b))
                elif _shared_side(pool[a], pool[b]) and \
                        token_set_ratio(pool[a]["sum"], pool[b]["sum"]) >= soft:
                    pairs.append((a, b))

        same = []
        if pairs:
            n_llm += len(pairs)
            # judge on the ORIGINAL post text (ground truth), not the LLM summary —
            # a summary can be wrong/cross-contaminated, the raw text never lies.
            pairs_text = [(_judge_text(pool[a]["rep"]), _judge_text(pool[b]["rep"])) for a, b in pairs]
            same = asyncio.run(_judge_pairs(pairs_text))   # PARALLEL within the day

        parent = list(range(len(pool)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for (a, b), s in zip(pairs, same):
            if s:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)

        merged = defaultdict(list)
        for k in range(len(pool)):
            merged[find(k)].extend(pool[k]["posts"])
        active = [_cluster_of(posts) for posts in merged.values()]

    # finalize everything still open
    for c in active:
        _create_event(run, task, c["posts"])
        n_events += 1

    logger.info("dedup(streaming): %d groups -> %d events, %d LLM judgments",
                len(clusters), n_events, n_llm)
    run.events_total = Event.objects.filter(run=run).count()
    run.events_corroborated = Event.objects.filter(run=run, channel_count__gte=2).count()
    run.save(update_fields=["events_total", "events_corroborated"])


# --------------------------------------------------------------------------- aggregate

def aggregate(run):
    events = (Event.objects.filter(run=run)
              .prefetch_related("tags").select_related("region_subject"))
    by_month, by_region = Counter(), Counter()
    by_cat = defaultdict(Counter)   # category -> Counter(tag name)
    for e in events:
        by_month[(e.event_date.strftime("%Y-%m") if e.event_date else "?")] += 1
        by_region[e.region_subject.name if e.region_subject else "?"] += 1
        for t in e.tags.all():
            by_cat[t.category][t.name] += 1
    run.stats = {
        "by_month": dict(sorted(by_month.items())),
        "by_region": dict(by_region.most_common(40)),
        "by_type": dict(by_cat["conflict"].most_common()),
        "by_nationality": dict(by_cat["nationality"].most_common(40)),
        "by_status": dict(by_cat["status"].most_common(20)),
        "by_role": dict(by_cat["role"].most_common(20)),
        "by_religion": dict(by_cat["religion"].most_common(20)),
        "by_group": dict(by_cat["group"].most_common(20)),
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
        "search_posts": run.task.search_posts,
        "search_comments": run.task.search_comments,
        "collect_chunk_days": run.task.collect_chunk_days,
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
