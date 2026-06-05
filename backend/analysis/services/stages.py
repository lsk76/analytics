"""
Stage-machine processors (task-scoped, resumable, claim-based).

Each Post carries a `stage`; a worker processes only posts at its input stage and
advances them. Reference data and posts/events live on the TASK (not a run), so
collection + analysis are continuous and resumable.

  collect      CollectChunk -> Post(stage=collected)        (single worker, TeleZip 2-parallel)
  enrich       collected    -> enriched                      (N workers, row locks)
  precluster   enriched     -> preclustered (dedup_group)    (windowed, watermark)
  classify     preclustered -> classified                    (N workers, on group reps)
  dedup        classified   -> deduped/done (-> Event)        (windowed, watermark)

Windowed stages use a watermark: a day D is processed only once its ±window
neighbourhood is fully collected & at the required stage (so cross-day merges are
stable). Posts older than two clusters never merge — clustering is local to `win`.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone as djtz

from analysis.models import Post, Event, Channel, CollectChunk
from .telezip import TelezipClient
from . import llm
from .normalize import resolve_region, resolve_in_category
from analysis.models import Tag
from . import pipeline as P  # reuse pure helpers
from rapidfuzz.fuzz import token_set_ratio

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = timedelta(minutes=20)   # stale claim is reclaimable after this
ENRICH_BATCH = 300
CLASSIFY_GROUP_BATCH = 400             # group-reps per classify tick


# --------------------------------------------------------------------------- claim helpers

def _claim_posts(task, stage, limit):
    """Atomically claim up to `limit` posts at `stage` (skip rows locked by peers)."""
    cutoff = djtz.now() - LOCK_TIMEOUT
    with transaction.atomic():
        ids = list(
            Post.objects.filter(task=task, stage=stage)
            .filter(Q(stage_locked_at__isnull=True) | Q(stage_locked_at__lt=cutoff))
            .order_by("posted_at", "id")
            .select_for_update(skip_locked=True)
            .values_list("id", flat=True)[:limit]
        )
        if ids:
            Post.objects.filter(id__in=ids).update(stage_locked_at=djtz.now())
    return ids


def _advance(post_ids, to_stage):
    Post.objects.filter(id__in=post_ids).update(
        stage=to_stage, stage_locked_at=None, stage_error="")


# --------------------------------------------------------------------------- collect

def _claim_chunk(task):
    cutoff = djtz.now() - LOCK_TIMEOUT
    with transaction.atomic():
        # claim a pending chunk OR reclaim a 'running' one abandoned by a dead worker
        chunk = (CollectChunk.objects
                 .filter(task=task)
                 .filter(Q(status="pending")
                         | Q(status="running", locked_at__lt=cutoff))
                 .select_for_update(skip_locked=True)
                 .order_by("date_from").first())
        if chunk:
            chunk.status = "running"
            chunk.locked_at = djtz.now()
            chunk.attempts += 1
            chunk.save(update_fields=["status", "locked_at", "attempts"])
    return chunk


async def _fetch_range(task, dfrom, dto):
    async with TelezipClient(settings.TELEZIP_API_KEY, settings.TELEZIP_BASE_URL) as tz:
        return await tz.find_posts(task.telezip_query, dfrom, dto,
                                   task.languages or None, unique=task.telezip_unique)


# IMPORTANT: TeleZip is touched ONLY by the collect worker, and the TelezipClient
# itself caps concurrency at TELEZIP_MAX_CONCURRENCY — so the global connection
# limit holds no matter how many gathers we launch. The enrich stage must NOT call TeleZip.

async def _fetch_channels(cids):
    """Fetch channel metadata from TeleZip. Concurrency is bounded by the client gate."""
    res = {}
    async with TelezipClient(settings.TELEZIP_API_KEY, settings.TELEZIP_BASE_URL) as tz:
        async def one(cid):
            res[cid] = await tz.get_channel(cid)
        await asyncio.gather(*[one(c) for c in cids])
    return res


def _cache_channels(task, cids):
    """Ensure Channel rows exist for these TeleZip channel ids (collector only)."""
    have = set(Channel.objects.filter(tg_id__in=cids).values_list("tg_id", flat=True))
    missing = [c for c in cids if c and c not in have]
    if not missing:
        return
    for cid, m in (asyncio.run(_fetch_channels(missing)) or {}).items():
        if not m:
            continue
        Channel.objects.update_or_create(
            tg_id=m["tg_id"] or cid,
            defaults={
                "username": m["username"], "title": m["title"], "description": m["about"],
                "subscribers": m["subscribers"] or 0, "language": m["language"],
                "is_channel": m["is_channel"], "enriched": True, "fetched_at": djtz.now(),
            },
        )


def _split_chunk(chunk):
    """Break a multi-day chunk into 1-day chunks (adaptive fallback on failure)."""
    d = chunk.date_from
    made = 0
    while d <= chunk.date_to:
        CollectChunk.objects.create(task=chunk.task, job=chunk.job,
                                    date_from=d, date_to=d, status="pending")
        d += timedelta(days=1)
        made += 1
    chunk.status = "split"
    chunk.locked_at = None
    chunk.save(update_fields=["status", "locked_at"])
    return made


def collect_once(task):
    """Process ONE pending CollectChunk. Returns True if it did work."""
    chunk = _claim_chunk(task)
    if not chunk:
        return False
    dfrom = datetime.combine(chunk.date_from, datetime.min.time(), tzinfo=timezone.utc)
    dto = datetime.combine(chunk.date_to, datetime.max.time(), tzinfo=timezone.utc)
    try:
        rows = asyncio.run(_fetch_range(task, dfrom, dto))
    except Exception as e:  # noqa: BLE001 — TeleZip outage / connection reset
        logger.warning("collect %s..%s failed: %s", chunk.date_from, chunk.date_to, e)
        if chunk.date_to > chunk.date_from:           # multi-day -> split to 1-day chunks
            n = _split_chunk(chunk)
            logger.info("split chunk into %d daily chunks", n)
        else:                                          # already 1 day -> retry later
            chunk.status = "failed" if chunk.attempts >= 4 else "pending"
            chunk.locked_at = None
            chunk.error = str(e)[:1000]
            chunk.save(update_fields=["status", "locked_at", "error"])
            if chunk.status == "failed":      # terminal chunk -> job may now be done
                _maybe_finish_job(chunk.job)
        return True

    n = 0
    for p in rows:
        url = p.get("message_url")
        if not url:
            continue
        post, _ = Post.objects.update_or_create(
            task=task, url=url,
            defaults={
                "channel_name": p.get("channel_name") or "",
                "telezip_date": P._post_dt(p.get("date")),
                "posted_at": P._post_dt(p.get("date")),
                "text": p.get("content") or "",
                "content_hash": p.get("content_hash") or "",
                "telezip_mid": p.get("mid"),
            },
        )
        if p.get("channel_id"):
            post.classification = {**(post.classification or {}), "_tz_channel_id": p["channel_id"]}
            post.save(update_fields=["classification"])
        n += 1

    # cache channel metadata for the channels we just saw (collector = only TeleZip user)
    cids = {p.get("channel_id") for p in rows if p.get("channel_id")}
    if cids:
        try:
            _cache_channels(task, list(cids))
        except Exception as e:  # noqa: BLE001 — don't lose collected posts over channel meta
            logger.warning("channel meta fetch failed (will link from cache later): %s", e)

    chunk.status = "done"
    chunk.posts_collected = n
    chunk.locked_at = None
    chunk.finished_at = djtz.now()
    chunk.save(update_fields=["status", "posts_collected", "locked_at", "finished_at"])
    logger.info("collect %s..%s: +%d posts", chunk.date_from, chunk.date_to, n)
    _maybe_finish_job(chunk.job)
    return True


def _maybe_finish_job(job):
    """Flip a collect job to 'collected' once none of its chunks are still pending/running.
    The collector only owns the collection phase; downstream stages run on the task."""
    if not job or job.status in ("collected", "done", "cancelled"):
        return
    chunks = CollectChunk.objects.filter(job=job)
    if not chunks.filter(status__in=["pending", "running"]).exists():
        from django.db.models import Sum
        job.status = "collected"
        job.posts_collected = chunks.aggregate(s=Sum("posts_collected"))["s"] or 0
        job.finished_at = djtz.now()
        job.save(update_fields=["status", "posts_collected", "finished_at"])
        logger.info("collect job #%s -> collected (%d posts)", job.id, job.posts_collected)


# --------------------------------------------------------------------------- enrich

def enrich_once(task):
    """No TeleZip here (collector owns it) — link channel from cache + LLM region."""
    ids = _claim_posts(task, Post.STAGE_COLLECTED, ENRICH_BATCH)
    if not ids:
        return False
    posts = list(Post.objects.filter(id__in=ids))
    cid_by_post = {p.id: (p.classification or {}).get("_tz_channel_id") for p in posts}
    unique_cids = {c for c in cid_by_post.values() if c}

    chan_by_cid = {c.tg_id: c for c in Channel.objects.filter(tg_id__in=unique_cids)}

    to_infer = ([(cid, ch.title, ch.description) for cid, ch in chan_by_cid.items()
                 if not ch.inferred_region and (ch.title or ch.description)]
                if task.geo_enabled else [])
    if to_infer:
        for cid, region in (asyncio.run(P._infer_regions(to_infer)) or {}).items():
            ch = chan_by_cid.get(cid)
            if ch and region:
                ch.inferred_region = region
                ch.save(update_fields=["inferred_region"])

    changed = []
    for p in posts:
        ch = chan_by_cid.get(cid_by_post.get(p.id))
        if ch and p.channel_id != ch.id:
            p.channel = ch
            changed.append(p)
    if changed:
        Post.objects.bulk_update(changed, ["channel"], batch_size=500)

    _advance(ids, Post.STAGE_ENRICHED)
    logger.info("enrich: +%d posts", len(ids))
    return True


# --------------------------------------------------------------------------- watermark

def _collection_frontier(task):
    """Earliest date still IN PROGRESS (pending/running). 'failed' is terminal — a
    permanently failed range must NOT block downstream stages forever, so we treat
    it as 'collected as far as it will get' and proceed with whatever data exists."""
    pend = (CollectChunk.objects.filter(task=task, status__in=["pending", "running"])
            .order_by("date_from").first())
    return pend.date_from if pend else None


def _stage_frontier(task, stages):
    """Earliest posted date among posts still at one of `stages` (work not yet done)."""
    p = (Post.objects.filter(task=task, stage__in=stages, posted_at__isnull=False)
         .order_by("posted_at").first())
    return p.posted_at.date() if p else None


def _ready_through(task, pending_stages):
    """Latest day D such that days <= D are fully collected AND past `pending_stages`.
    None means 'nothing blocks' (everything collected & processed)."""
    fronts = [f for f in (_collection_frontier(task),
                          _stage_frontier(task, pending_stages)) if f]
    return (min(fronts) - timedelta(days=1)) if fronts else None


# --------------------------------------------------------------------------- precluster (windowed)

def precluster_once(task):
    win = timedelta(days=task.dedup_window_days)
    ready = _ready_through(task, [Post.STAGE_COLLECTED])   # collected->enriched frontier
    # day D is settled for precluster when D + win <= ready (neighbours up to D+win enriched)
    settle_to = (ready - win) if ready else None

    # in-scope filter: posts-only keeps ONLY confirmed channels (is_channel=True) —
    # chats and unknown/unlinked-channel posts are dropped (no reach, low signal);
    # comments-only is the inverse. antiscope is the exact complement so out-of-scope
    # posts are finalized and never clog the watermark.
    both = (task.search_posts and task.search_comments) or \
           (not task.search_posts and not task.search_comments)
    if both:
        scope, antiscope = Q(), None
    elif task.search_posts:
        scope = Q(channel__is_channel=True)
        antiscope = ~scope                       # is_channel False OR NULL OR no channel
    else:  # comments only
        scope = Q(channel__is_channel=False)
        antiscope = ~scope

    # finalize OUT-OF-SCOPE enriched posts so they never clog the dedup watermark
    finalized = 0
    if antiscope is not None:
        out = Post.objects.filter(task=task, stage=Post.STAGE_ENRICHED).filter(antiscope)
        if settle_to is not None:
            out = out.filter(posted_at__date__lte=settle_to)
        finalized = out.update(stage=Post.STAGE_DONE, stage_locked_at=None)

    base = Post.objects.filter(task=task, stage=Post.STAGE_ENRICHED,
                               posted_at__isnull=False).filter(scope)
    newq = base if settle_to is None else base.filter(posted_at__date__lte=settle_to)
    newp = list(newq.order_by("posted_at", "id"))
    if not newp:
        return finalized > 0

    # back-buffer: already-preclustered (or later) neighbours within `win` for cross-day merges
    oldest = newp[0].posted_at - win
    anchors = list(
        Post.objects.filter(task=task, posted_at__gte=oldest, posted_at__lt=newp[0].posted_at)
        .exclude(stage__in=[Post.STAGE_COLLECTED, Post.STAGE_ENRICHED])
        .exclude(dedup_group__isnull=True)
        .order_by("posted_at", "id")
    )

    items = anchors + newp
    n = len(items)
    # group id per item: anchors carry their existing dedup_group; new items default to own id
    gid = [(p.dedup_group if i < len(anchors) else p.id) for i, p in enumerate(items)]
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

    by_hash = defaultdict(list)
    for i, p in enumerate(items):
        if p.content_hash:
            by_hash[p.content_hash].append(i)
    for g in by_hash.values():
        for k in g[1:]:
            union(g[0], k)

    norms = [P._norm(p.text) for p in items]
    for i in range(n):
        for j in range(i + 1, n):
            if items[j].posted_at - items[i].posted_at > win:
                break
            if token_set_ratio(norms[i], norms[j]) >= task.dedup_pre_thresh:
                union(i, j)

    # resolve each union-set to a single group id (prefer an anchor's existing id = smallest)
    set_gid = {}
    for i in range(n):
        r = find(i)
        cand = gid[i]
        if r not in set_gid or cand < set_gid[r]:
            set_gid[r] = cand

    for i in range(len(anchors), n):     # only the NEW posts get written/advanced
        newp_i = items[i]
        newp_i.dedup_group = set_gid[find(i)]
    Post.objects.bulk_update(newp, ["dedup_group"], batch_size=500)
    _advance([p.id for p in newp], Post.STAGE_PRECLUSTERED)
    logger.info("precluster: settled %d posts (settle_to=%s)", len(newp), settle_to)
    return True


# --------------------------------------------------------------------------- classify

def build_classify_prompt(task):
    """Assemble the full classifier prompt: the task's DOMAIN rules + an auto JSON
    schema built from the task's chosen tag categories (+ geo fields if enabled)."""
    cats = list(task.tag_categories.all())
    geo = ('"region":"<суб\'єкт або порожньо>","settlement":"<місто/селище або порожньо>",'
           if task.geo_enabled else "")
    tag_fields = ",".join(f'"{c.key}":["..."]' for c in cats)
    schema = (
        "Поверни СТРОГО JSON-масив, по одному об'єкту на кожне вхідне повідомлення, "
        "у тому ж порядку:\n"
        f'[{{"i":<індекс>,"is_relevant":<true|false>,{geo}'
        f'"tags":{{{tag_fields}}},"summary":"<короткий опис інциденту, 1 речення>"}}]'
    )
    hints = ["Поля всередині tags — це СПИСКИ значень (0..N) для кожної категорії:"]
    for c in cats:
        if c.closed:
            seeded = list(Tag.objects.filter(category=c.key).values_list("name", flat=True))
            hints.append(f'- "{c.key}" ({c.label}): обери ТОЧНО зі списку {seeded}; '
                         f'якщо відповідного нема — пропусти (не вигадуй).')
        else:
            guide = c.hint or "вільні значення українською, узагальнено"
            hints.append(f'- "{c.key}" ({c.label}): {guide}.')
    geo_note = ("\nregion — суб'єкт (область/край/республіка) БЕЗ міста; "
                "settlement — місто/селище окремо (напр. region='Хабаровський край', "
                "settlement='Хабаровськ')." if task.geo_enabled else "")
    return "\n".join([task.classify_system_prompt.strip(), "", schema, *hints,
                      geo_note, "summary — лише про ЦЕ повідомлення. Лише валідний JSON, без markdown."])


def classify_once(task):
    # classify ONE rep per group; claim preclustered posts that are the group root
    ids = _claim_posts(task, Post.STAGE_PRECLUSTERED, CLASSIFY_GROUP_BATCH * 4)
    if not ids:
        return False
    posts = list(Post.objects.filter(id__in=ids))
    groups = defaultdict(list)
    for p in posts:
        groups[p.dedup_group].append(p)

    # rep = earliest in group; classify reps, propagate to claimed members
    reps = [sorted(m, key=lambda x: (x.posted_at or djtz.now(), x.id))[0] for m in groups.values()]
    batches = [reps[i:i + P.CLASSIFY_BATCH] for i in range(0, len(reps), P.CLASSIFY_BATCH)]
    text_batches = [[p.text for p in b] for b in batches]
    results = asyncio.run(P._classify_batches(build_classify_prompt(task), text_batches,
                                              task.llm_model or None))
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
            rep_cls[rep.dedup_group] = cls

    rfield = task.relevance_field
    for gkey, members in groups.items():
        cls = rep_cls.get(gkey, {})
        for p in members:
            c = dict(cls)
            keep_tz = (p.classification or {}).get("_tz_channel_id")
            if keep_tz is not None:
                c["_tz_channel_id"] = keep_tz
            p.classification = c
            p.is_classified = True
            p.is_relevant = bool(c.get(rfield))
    Post.objects.bulk_update(posts, ["classification", "is_classified", "is_relevant"],
                             batch_size=500)
    _advance(ids, Post.STAGE_CLASSIFIED)
    logger.info("classify: %d groups / %d posts", len(groups), len(posts))
    return True


# --------------------------------------------------------------------------- dedup (windowed)

def _same_region(a, b):
    """Clusters report the same place (normalized region string)."""
    ra, rb = a.get("region", ""), b.get("region", "")
    return bool(ra) and bool(rb) and token_set_ratio(ra, rb) >= 85


def _create_event(task, posts_in):
    """Materialize one finalized cluster of posts into a task Event."""
    posts_in = sorted(posts_in, key=lambda p: p.posted_at)
    head = posts_in[0]
    cls = head.classification or {}
    region, region_subject, settlement = "", None, ""
    if task.geo_enabled:                       # geolocation is per-task (off for non-geo tasks)
        region = (cls.get("region") or "").strip()
        sett_hint = (cls.get("settlement") or "").strip()
        if not region and head.channel and head.channel.inferred_region:
            region = head.channel.inferred_region
        loc = ", ".join(x for x in (sett_hint, region) if x)
        region_subject, settlement = resolve_region(loc) if loc else (None, "")
        if not settlement and sett_hint:
            settlement = sett_hint
    ev = Event.objects.create(
        task=task,
        event_date=head.posted_at.date(),
        region=region, region_subject=region_subject, settlement=settlement,
        summary=cls.get("summary") or head.text[:300],
        post_count=len(posts_in),
        is_corroborated=len({p.channel_id for p in posts_in if p.channel_id}) >= 2,
    )
    # resolve tags per the task's chosen categories (values are lists)
    tag_objs = []
    cls_tags = cls.get("tags") or {}
    for c in task.tag_categories.all():
        vals = cls_tags.get(c.key) or []
        if isinstance(vals, str):
            vals = [vals]
        for v in vals:
            if v and (o := resolve_in_category(str(v), c.key, c.closed)):
                tag_objs.append(o)
    if tag_objs:
        ev.tags.set(tag_objs)
    _attach_posts(ev, posts_in)
    return ev


def _attach_posts(ev, posts_in):
    """Link posts to event, advance them to done, recompute count/reach. Posts that
    dedup attaches are already judged the SAME incident (corroborating reposts), so an
    already-audited event is NOT sent back to review — that would just burn the pricier
    model for no new signal."""
    Post.objects.filter(id__in=[p.id for p in posts_in]).update(
        event=ev, stage=Post.STAGE_DONE, stage_locked_at=None)
    members = list(Post.objects.filter(event=ev).select_related("channel"))
    chans = {p.channel_id: (p.channel.subscribers or 0) for p in members if p.channel_id}
    ev.post_count = len(members)
    ev.reach = sum(chans.values())
    ev.is_corroborated = len(chans) >= 2
    ev.save(update_fields=["post_count", "reach", "is_corroborated"])


def dedup_once(task):
    win = timedelta(days=task.dedup_window_days)
    ready = _ready_through(
        task, [Post.STAGE_COLLECTED, Post.STAGE_ENRICHED, Post.STAGE_PRECLUSTERED])
    settle_to = (ready - win) if ready else None

    relq = Post.objects.filter(task=task, stage=Post.STAGE_CLASSIFIED,
                               is_relevant=True, dedup_group__isnull=False,
                               posted_at__isnull=False)
    if settle_to is not None:
        relq = relq.filter(posted_at__date__lte=settle_to)
    rel = list(relq.select_related("channel").order_by("posted_at", "id"))

    # non-relevant classified posts in the settled region are simply finalized
    irrel = Post.objects.filter(task=task, stage=Post.STAGE_CLASSIFIED)
    if settle_to is not None:
        irrel = irrel.filter(posted_at__date__lte=settle_to)
    irrel = irrel.exclude(id__in=[p.id for p in rel])
    irr_n = irrel.update(stage=Post.STAGE_DONE, stage_locked_at=None)

    if not rel:
        return irr_n > 0

    by_group = defaultdict(list)
    for p in rel:
        by_group[p.dedup_group].append(p)
    new_clusters = sorted((P._cluster_of(g) for g in by_group.values()),
                          key=lambda c: c["dmin"])

    # anchors: recent events (within win) we may still merge into
    oldest = new_clusters[0]["dmin"] - win
    recent_events = (Event.objects.filter(task=task, event_date__gte=oldest.date())
                     .prefetch_related("posts__channel"))
    active = []
    for ev in recent_events:
        eposts = [p for p in ev.posts.all() if p.posted_at]
        if eposts:
            c = P._cluster_of(eposts)
            c["event"] = ev
            active.append(c)

    soft = max(35, task.dedup_cand_thresh - 20)
    generic = task.generic_sides or None        # per-task umbrella terms
    pool = active + new_clusters
    base = len(active)
    pairs = []      # candidate pairs the LLM judge decides
    forced = []     # near-identical -> merge without (or despite) the judge
    for b in range(base, len(pool)):
        for a in range(0, b):
            if pool[b]["dmin"] - pool[a]["dmax"] > win and pool[a]["dmin"] - pool[b]["dmax"] > win:
                continue
            sf = token_set_ratio(pool[a]["sum"], pool[b]["sum"])
            tf = token_set_ratio(pool[a]["text"], pool[b]["text"])
            shared = P._shared_side(pool[a], pool[b], generic)
            # near-identical summary + same concrete nationality => same event
            # (overrides occasional judge errors, e.g. two reports of one court case)
            if sf >= 90 and shared:
                forced.append((a, b))
            elif max(tf, sf) >= task.dedup_cand_thresh:
                pairs.append((a, b))
            elif shared and sf >= soft:
                pairs.append((a, b))
            elif _same_region(pool[a], pool[b]) and sf >= max(45, soft):
                pairs.append((a, b))          # same place + moderate similarity

    same = []
    if pairs:
        pairs_text = [(P._judge_text(pool[a]["rep"]), P._judge_text(pool[b]["rep"]))
                      for a, b in pairs]
        same = asyncio.run(P._judge_pairs(pairs_text, system=task.dedup_judge_prompt or None))

    parent = list(range(len(pool)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for a, b in forced:
        union(a, b)
    for (a, b), s in zip(pairs, same):
        if s:
            union(a, b)

    merged = defaultdict(list)
    for k in range(len(pool)):
        merged[find(k)].append(k)

    created = 0
    for root, members in merged.items():
        idxs = sorted(members)
        anchor = next((i for i in idxs if i < base), None)
        new_posts = [p for i in idxs if i >= base for p in pool[i]["posts"]]
        if anchor is not None:                 # merge new posts into the existing event
            if new_posts:
                _attach_posts(pool[anchor]["event"], new_posts)
        else:                                  # brand-new event
            all_posts = [p for i in idxs for p in pool[i]["posts"]]
            _create_event(task, all_posts)
            created += 1
    logger.info("dedup: settled %d clusters -> +%d events (settle_to=%s)",
                len(new_clusters), created, settle_to)
    return True


# --------------------------------------------------------------------------- enqueue

def enqueue_collection(task, date_from, date_to, chunk_days=None, job=None):
    """Create pending CollectChunks covering [date_from, date_to], skipping ranges
    already fully collected. Returns the number of new chunks."""
    chunk_days = max(1, int(chunk_days or task.collect_chunk_days or 3))
    made = 0
    d = date_from
    while d <= date_to:
        end = min(d + timedelta(days=chunk_days - 1), date_to)
        # skip if this exact range is already covered by a done/pending chunk
        exists = CollectChunk.objects.filter(
            task=task, date_from=d, date_to=end,
            status__in=["pending", "running", "done"]).exists()
        if not exists:
            CollectChunk.objects.create(task=task, job=job, date_from=d, date_to=end,
                                        status="pending")
            made += 1
        d = end + timedelta(days=1)
    return made


def reprocess_period(task, date_from, date_to):
    """Re-run the pipeline on ALREADY-collected posts (no TeleZip): delete the period's
    events and reset its posts back to 'collected'. Workers redo enrich→…→dedup.
    `classification` is kept (it carries the cached TeleZip channel id; it's overwritten
    at classify anyway). Returns (events_deleted, posts_reset)."""
    ev = Event.objects.filter(task=task, event_date__gte=date_from, event_date__lte=date_to)
    n_ev = ev.count()
    ev.delete()
    n_posts = (Post.objects.filter(task=task, posted_at__date__gte=date_from,
                                   posted_at__date__lte=date_to)
               .update(stage=Post.STAGE_COLLECTED, stage_locked_at=None, stage_attempts=0,
                       stage_error="", event=None, dedup_group=None,
                       is_classified=False, is_relevant=None))
    logger.info("reprocess %s..%s: -%d events, reset %d posts", date_from, date_to, n_ev, n_posts)
    return n_ev, n_posts


def recollect_fresh(task, date_from, date_to, job=None):
    """Wipe the period entirely (events + posts + chunks) and re-enqueue collection from
    TeleZip. Returns (events_deleted, posts_deleted, chunks_added)."""
    ev = Event.objects.filter(task=task, event_date__gte=date_from, event_date__lte=date_to)
    n_ev = ev.count(); ev.delete()
    posts = Post.objects.filter(task=task, posted_at__date__gte=date_from,
                                posted_at__date__lte=date_to)
    n_posts = posts.count(); posts.delete()
    CollectChunk.objects.filter(task=task, date_from__gte=date_from,
                                date_to__lte=date_to).delete()
    n_chunks = enqueue_collection(task, date_from, date_to, job=job)
    logger.info("recollect %s..%s: -%d events, -%d posts, +%d chunks",
                date_from, date_to, n_ev, n_posts, n_chunks)
    return n_ev, n_posts, n_chunks


# --------------------------------------------------------------------------- registry

def review_once(task):
    # imported lazily: review.py imports helpers from this module (avoid import cycle)
    from analysis.services.review import review_once as _r
    return _r(task)


STAGE_RUNNERS = {
    "collect": collect_once,
    "enrich": enrich_once,
    "precluster": precluster_once,
    "classify": classify_once,
    "dedup": dedup_once,
    "review": review_once,
}
