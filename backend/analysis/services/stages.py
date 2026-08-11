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
from django.db import transaction, DatabaseError, IntegrityError
from django.db.models import F, Q
from django.utils import timezone as djtz

from analysis.models import Post, Event, Channel, CollectChunk
from .telezip import TelezipClient
from . import llm
from .normalize import resolve_region, resolve_in_category
from analysis.models import Tag
from . import pipeline as P  # reuse pure helpers
from rapidfuzz.fuzz import token_set_ratio
from rapidfuzz import process as rf_process
import numpy as np

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = timedelta(minutes=20)   # stale claim is reclaimable after this
ENRICH_BATCH = 300
CLASSIFY_GROUP_BATCH = 400             # group-reps per classify tick
CLASSIFY_MAX_ATTEMPTS = 4              # скільки разів повторювати батч без вердикту LLM
PRECLUSTER_WINDOW_DAYS = 1             # each precluster tick claims this many days of posts.
                                       # Even with cdist-vectorised fuzzy, the matrix is N²:
                                       # measured 5k posts/day @ avg 841-char texts → ~30s/tick
                                       # at window=1. window=3 → 15k posts → 9× more comparisons
                                       # → 10+ min/tick (texts are long, so SIMD savings are
                                       # bounded by tokenisation cost). Single-worker setup with
                                       # window=1 + anchor back-buffer (dedup_window_days neighbours)
                                       # still catches cross-day duplicates correctly. Re-raise once
                                       # we have the PreclusterClaim catalog table for parallel workers.


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
    now = djtz.now()
    cutoff = now - LOCK_TIMEOUT
    with transaction.atomic():
        # claim a pending chunk OR reclaim a 'running' one abandoned by a dead worker.
        # `next_retry_at` is the cooldown gate after a transient error (None = ready now).
        chunk = (CollectChunk.objects
                 .filter(task=task)
                 .filter(Q(status="pending")
                         | Q(status="running", locked_at__lt=cutoff))
                 .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
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


# Substrings that mark a TRANSIENT error worth endless retry. Anything else (HTTP 4xx,
# parse, auth) is permanent and should fail after a few attempts to avoid masking bugs.
_TRANSIENT_MARKERS = (
    "Cannot connect to host", "Connection reset", "Connection refused",
    "TimeoutError", "Timeout", "timed out",
    "Name or service not known", "Temporary failure in name",
    "ServerDisconnected", "ClientConnectorError", "ClientOSError",
    "SSL", "ssl:",
    "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "HTTP 429",
    # TelezipClient raises RuntimeError("TeleZip <status>") — match that exact
    # shape too, else persistent 429/5xx throttling is misread as PERMANENT and a
    # chunk gets failed after 4 attempts instead of retried forever with backoff.
    "TeleZip 429", "TeleZip 500", "TeleZip 502", "TeleZip 503", "TeleZip 504",
)


def _is_transient_error(e: Exception) -> bool:
    msg = f"{type(e).__name__}: {e}"
    return any(m in msg for m in _TRANSIENT_MARKERS)


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
        else:                                          # 1 day -> retry policy
            transient = _is_transient_error(e)
            chunk.attempts = (chunk.attempts or 0) + 1
            chunk.locked_at = None
            chunk.error = str(e)[:1000]
            if transient:
                # endless retry for connection/DNS/timeout/5xx — backoff 15s → 30s → 60s cap
                delay = (15, 30, 60)[min(chunk.attempts - 1, 2)]
                chunk.status = "pending"
                chunk.next_retry_at = djtz.now() + timedelta(seconds=delay)
                chunk.save(update_fields=["status", "attempts", "locked_at",
                                          "error", "next_retry_at"])
                logger.info("collect %s: transient error, retry in %ds (attempt %d)",
                            chunk.date_from, delay, chunk.attempts)
            else:
                # permanent error (4xx, parse, auth) — fail after 4 attempts
                chunk.status = "failed" if chunk.attempts >= 4 else "pending"
                chunk.save(update_fields=["status", "attempts", "locked_at", "error"])
                if chunk.status == "failed":
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
    chunk.next_retry_at = None        # clear any backoff from prior transient failures
    chunk.finished_at = djtz.now()
    chunk.save(update_fields=["status", "posts_collected", "locked_at",
                              "next_retry_at", "finished_at"])
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

def channel_post_hashes(hashes):
    """Subset of `hashes` that ALSO exist as a CHANNEL (broadcast) post. Used to detect
    chat messages that merely echo a channel post (linked-channel auto-forward or a
    forwarded channel post) — those are not genuine member activity."""
    hashes = list(hashes)
    if not hashes:
        return set()
    return set(Post.objects.filter(content_hash__in=hashes, channel__chat_type="channel")
               .values_list("content_hash", flat=True))


def flag_channel_reposts(post_ids):
    """Mark chat/discussion posts in this batch whose content_hash also exists as a
    channel post as `is_channel_repost` (exclude from chat-activity analysis). Works
    BOTH directions so collection order is irrelevant: chat posts that match a known
    channel post, AND channel posts in the batch that retro-flag already-stored chat
    echoes of the same hash. Cheap — content_hash is indexed."""
    posts = list(Post.objects.filter(id__in=post_ids).select_related("channel"))
    chat = [p for p in posts if p.channel and p.channel.chat_type in ("chat", "discussion")]
    chan_hashes = [p.content_hash for p in posts
                   if p.channel and p.channel.chat_type == "channel"]
    if chat:
        known = channel_post_hashes({p.content_hash for p in chat})
        flag = [p.id for p in chat if p.content_hash in known]
        if flag:
            Post.objects.filter(id__in=flag).update(is_channel_repost=True)
    if chan_hashes:
        (Post.objects.filter(content_hash__in=chan_hashes, is_channel_repost=False,
                             channel__chat_type__in=["chat", "discussion"])
         .update(is_channel_repost=True))


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

    flag_channel_reposts(ids)          # channel echoes in chats -> is_channel_repost

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

    # Optional opt-in (task.drop_linked_comments): TeleZip labels posts pulled from a
    # channel's CLOSED linked discussion group with a "linked:" username prefix. These
    # are comments/reactions, noise for an event pipeline — exclude them from scope and
    # add them to antiscope so they finalize to DONE instead of clogging dedup.
    if getattr(task, "drop_linked_comments", False):
        linked = Q(channel__username__startswith="linked:")
        scope = scope & ~linked
        antiscope = linked if antiscope is None else (antiscope | linked)

    # Optional opt-in (task.min_channel_subscribers > 0): drop posts from MICRO channels
    # (subscriber count below the threshold). This is the bot-farm amplification cloud —
    # one story mass-reposted across ~1800 channels of ~50 subscribers each, which inflates
    # post_count/reach without real audience. Only ENRICHED channels are filtered (we know
    # their real size); unenriched/unknown (subscribers still default 0) are KEPT so a
    # not-yet-enriched legit channel isn't dropped on missing data. Dropped -> antiscope ->
    # finalized to DONE before the expensive classify/dedup, so micro spam is never LLM-analysed.
    min_subs = getattr(task, "min_channel_subscribers", 0) or 0
    if min_subs > 0:
        micro = Q(channel__enriched=True, channel__subscribers__lt=min_subs)
        scope = scope & ~micro
        antiscope = micro if antiscope is None else (antiscope | micro)

    # finalize OUT-OF-SCOPE enriched posts so they never clog the dedup watermark
    finalized = 0
    if antiscope is not None:
        out = Post.objects.filter(task=task, stage=Post.STAGE_ENRICHED).filter(antiscope)
        if settle_to is not None:
            out = out.filter(posted_at__date__lte=settle_to)
        finalized = out.update(stage=Post.STAGE_DONE, stage_locked_at=None)

    # CLAIM a contiguous DATE RANGE of enriched posts. Each worker takes a 14-day
    # window starting from the earliest yet-unlocked post. With multiple workers,
    # everyone gets disjoint date ranges (because the FIRST row's FOR-UPDATE-SKIP-LOCKED
    # serialises the choice of starting date). Inside its own range a worker runs the
    # full union-find with anchors — so cross-day merges within the range are correct.
    # The only risk zone is the day boundary BETWEEN two adjacent workers' ranges:
    # mitigated by (a) sonnet audit, (b) the next precluster pass picking up any
    # newly-enriched posts in the boundary days and seeing the now-preclustered neighbours
    # as anchors.
    cutoff = djtz.now() - LOCK_TIMEOUT
    base = (Post.objects.filter(task=task, stage=Post.STAGE_ENRICHED,
                                posted_at__isnull=False).filter(scope)
            .filter(Q(stage_locked_at__isnull=True) | Q(stage_locked_at__lt=cutoff)))
    if settle_to is not None:
        base = base.filter(posted_at__date__lte=settle_to)
    with transaction.atomic():
        earliest = (base.select_for_update(skip_locked=True)
                    .order_by("posted_at", "id").first())
        if not earliest:
            return finalized > 0
        d_from = earliest.posted_at.date()
        d_to = d_from + timedelta(days=PRECLUSTER_WINDOW_DAYS)
        # Lock the entire date window — other workers' SELECT FOR UPDATE SKIP LOCKED
        # will skip these rows and pick the next available date range.
        window_qs = (Post.objects.filter(task=task, stage=Post.STAGE_ENRICHED,
                                          posted_at__isnull=False)
                     .filter(scope)
                     .filter(posted_at__date__gte=d_from, posted_at__date__lt=d_to))
        if settle_to is not None:
            window_qs = window_qs.filter(posted_at__date__lte=settle_to)
        n_locked = window_qs.update(stage_locked_at=djtz.now())
    if not n_locked:
        return finalized > 0
    newp = list(window_qs.order_by("posted_at", "id"))
    logger.info("precluster: claimed %d posts in window %s..%s",
                len(newp), d_from, d_to - timedelta(days=1))

    # back-buffer: ONE representative per dedup_group from the prior `win` days. We only
    # need one anchor per existing cluster because all members share text/content_hash —
    # comparing the newp against the rep is equivalent to comparing against every member,
    # but keeps the cdist matrix small. Without this, anchors can balloon to 10-15× the
    # newp count (e.g. on 27.09: 5.4k newp + 16k anchors = 21k items → 16× the work).
    oldest = newp[0].posted_at - win
    anchors_qs = (Post.objects.filter(task=task, posted_at__gte=oldest, posted_at__lt=newp[0].posted_at)
                  .exclude(stage__in=[Post.STAGE_COLLECTED, Post.STAGE_ENRICHED])
                  .exclude(dedup_group__isnull=True))
    # group by dedup_group, pick the earliest member as the representative
    seen_groups = set()
    anchors = []
    for p in anchors_qs.order_by("dedup_group", "posted_at", "id"):
        if p.dedup_group in seen_groups:
            continue
        seen_groups.add(p.dedup_group)
        anchors.append(p)
    # keep the rest of the code's assumption that anchors are time-ordered
    anchors.sort(key=lambda p: (p.posted_at, p.id))

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
    # Vectorised pairwise fuzzy via rapidfuzz.process.cdist — runs the same
    # token_set_ratio comparisons but in C/SIMD with score_cutoff filtering, so
    # entries below `dedup_pre_thresh` collapse to 0 inside the matrix.
    # Measured on day=2025-09-27 (5449 posts, avg_len=841): Python nested loop = 248s,
    # cdist = 27s (~9× faster), and produces IDENTICAL clusters (45 multi-clusters,
    # 5379 merged_posts, 70 singletons in both). Cutover date for this implementation:
    # 2025-09-27 — if cdist ever produces corrupted clusters, release locks on
    # posts.posted_at >= 2025-09-27 and reprocess.
    win_secs = win.total_seconds()
    posted = np.array([p.posted_at.timestamp() for p in items])
    mat = rf_process.cdist(norms, norms, scorer=token_set_ratio,
                           score_cutoff=task.dedup_pre_thresh, workers=-1)
    # Iterate non-zero upper-triangle entries; restrict to pairs within `win` days.
    ii, jj = np.where(mat >= task.dedup_pre_thresh)
    for i_, j_ in zip(ii, jj):
        i, j = int(i_), int(j_)
        if j <= i:
            continue
        if abs(posted[j] - posted[i]) > win_secs:
            continue
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
    # Батч, який НЕ повернув вердикт (мережевий збій/таймаут LLM), НЕ вважається
    # опрацьованим: інакше пости тихо лягають як is_relevant=False назавжди
    # (одноразовий Connection error → мовчазна втрата половини місяця).
    # Такі групи лишаємо на STAGE_PRECLUSTERED зі stage_error — воркер добере їх
    # наступним проходом, а збій видно в адмінці.
    rep_cls = {}
    failed_groups = set()
    for bi, batch in enumerate(batches):
        arr = results.get(bi) or []
        by_i = {}
        for k, obj in enumerate(arr):
            if isinstance(obj, dict):
                by_i[obj.get("i", k)] = obj
        for j, rep in enumerate(batch):
            cls = by_i.get(j)
            if not isinstance(cls, dict) or "is_relevant" not in cls:
                failed_groups.add(rep.dedup_group)
                continue
            cls.pop("i", None)
            rep_cls[rep.dedup_group] = cls

    done_posts, retry_ids = [], []
    for gkey, members in groups.items():
        if gkey in failed_groups:
            retry_ids.extend(p.id for p in members)
            continue
        cls = rep_cls.get(gkey, {})
        for p in members:
            c = dict(cls)
            keep_tz = (p.classification or {}).get("_tz_channel_id")
            if keep_tz is not None:
                c["_tz_channel_id"] = keep_tz
            p.classification = c
            p.is_classified = True
            p.is_relevant = bool(c.get("is_relevant"))
            done_posts.append(p)
    Post.objects.bulk_update(done_posts, ["classification", "is_classified", "is_relevant"],
                             batch_size=500)
    _advance([p.id for p in done_posts], Post.STAGE_CLASSIFIED)
    if retry_ids:
        Post.objects.filter(id__in=retry_ids).update(
            stage=Post.STAGE_PRECLUSTERED, stage_locked_at=None,
            stage_attempts=F("stage_attempts") + 1,
            stage_error="classify: LLM не повернув вердикт — батч на повтор")
        # Після CLASSIFY_MAX_ATTEMPTS припиняємо крутити той самий батч (інакше
        # стабільно зламаний пост палить LLM-виклики вічно). Пост іде далі як
        # нерелевантний, АЛЕ зі stage_error — втрата лишається видимою, не тихою.
        stuck = list(Post.objects.filter(id__in=retry_ids,
                                         stage_attempts__gte=CLASSIFY_MAX_ATTEMPTS)
                     .values_list("id", flat=True))
        if stuck:
            Post.objects.filter(id__in=stuck).update(
                stage=Post.STAGE_CLASSIFIED, is_classified=True, is_relevant=False,
                stage_locked_at=None,
                stage_error=f"classify: без вердикту після {CLASSIFY_MAX_ATTEMPTS} спроб")
            logger.error("classify: %d постів здались після %d спроб — позначені "
                         "нерелевантними зі stage_error", len(stuck), CLASSIFY_MAX_ATTEMPTS)
        logger.warning("classify: %d постів (%d груп) без вердикту LLM → на повтор",
                       len(retry_ids) - len(stuck), len(failed_groups))
    logger.info("classify: %d groups / %d posts", len(groups) - len(failed_groups),
                len(done_posts))
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
    uniq_channels = {p.channel_id for p in posts_in if p.channel_id}
    ev = Event.objects.create(
        task=task,
        event_date=head.posted_at.date(),
        region=region, region_subject=region_subject, settlement=settlement,
        summary=cls.get("summary") or head.text[:300],
        post_count=len(posts_in),
        channel_count=len(uniq_channels),
    )
    # resolve tags per the task's chosen categories (values are lists)
    tag_objs = []
    cls_tags = cls.get("tags") or {}
    from analysis.services import tags as tag_service
    for c in task.tag_categories.all():
        vals = cls_tags.get(c.key) or []
        if isinstance(vals, str):
            vals = [vals]
        for v in vals:
            # єдиний сервіс тегів: closed-флаг бере з TagCategory, а не з call-site
            if v and (o := tag_service.resolve(c.key, str(v))):
                tag_objs.append(o)
    if tag_objs:
        ev.tags.set(tag_objs)
    # Гео як тег (спільна категорія events+monitor): місто → settlement-тег.
    # Дзеркалить Event.settlement (поле лишається legacy до Фази 3).
    if settlement and (st := tag_service.resolve("settlement", settlement)):
        ev.tags.add(st)
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
    ev.channel_count = len(chans)
    ev.reach = sum(chans.values())
    ev.save(update_fields=["post_count", "channel_count", "reach"])


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

    # PER-TICK DATE CAP: process at most DEDUP_BATCH_DAYS of the OLDEST eligible
    # posts per tick. Live collection keeps each tick small naturally, but a bulk
    # re-dedup (e.g. 84k posts re-queued at once after detaching event tails) would
    # otherwise try to fuzzy+judge the whole backlog in ONE tick — pathological. The
    # cap (>> dedup_window_days) keeps within-window cross-day merges intact while the
    # worker marches forward day by day.
    # NB: do NOT drop this below the typical viral-repost spread — at 1 day the anchor
    # window (event_date ± dedup_window_days) ages a fresh event out after 2 days, so a
    # repost 3+ days after the first mention spawns a DUPLICATE event instead of merging.
    # Measured 2026-06-14: batch=1 fragmented the detached-tail backfill into ~8k events
    # of which only ~1.5k were unique. Keep >= a week.
    DEDUP_BATCH_DAYS = 7
    oldest_day = relq.order_by("posted_at").values_list("posted_at", flat=True).first()
    if oldest_day is not None:
        cap_to = oldest_day.date() + timedelta(days=DEDUP_BATCH_DAYS)
        relq = relq.filter(posted_at__date__lte=cap_to)
    rel = list(relq.select_related("channel").order_by("posted_at", "id"))

    # Non-relevant classified posts can be finalized WITHOUT the settle_to gate:
    # they don't participate in dedup and can't ever join a relevant cluster (classify
    # propagates the same `is_relevant` to every member of a dedup_group). Holding
    # them back behind the watermark only created visible "stuck classified" backlog
    # when precluster on a long backfill is slow to advance the frontier.
    irrel = (Post.objects.filter(task=task, stage=Post.STAGE_CLASSIFIED, is_relevant=False)
             .exclude(id__in=[p.id for p in rel]))
    irr_n = irrel.update(stage=Post.STAGE_DONE, stage_locked_at=None)

    if not rel:
        return irr_n > 0

    by_group = defaultdict(list)
    for p in rel:
        by_group[p.dedup_group].append(p)
    # Hard cap on cluster date-spread: split a `dedup_group` into sub-clusters whenever
    # the gap between consecutive posts exceeds MAX_CLUSTER_GAP_DAYS. Without this, the
    # precluster anchor-chain (A↔B within 2d, B↔C within 2d, …) can carry one `dedup_group`
    # across many months — same SEO-spam template, or different real incidents that share
    # wording stitched together via intermediaries. The cap reins both:
    # - spam-farm clusters keep one Event (first burst) and don't accumulate forever;
    # - chained over-merges across distinct incidents split into separate Events.
    MAX_CLUSTER_GAP_DAYS = 14
    split_groups = []
    for posts in by_group.values():
        posts.sort(key=lambda p: p.posted_at)
        cur = [posts[0]]
        for p in posts[1:]:
            if (p.posted_at - cur[-1].posted_at).days > MAX_CLUSTER_GAP_DAYS:
                split_groups.append(cur); cur = []
            cur.append(p)
        split_groups.append(cur)
    new_clusters = sorted((P._cluster_of(g) for g in split_groups),
                          key=lambda c: c["dmin"])
    # CAP new clusters per tick. The pair-build below is O(new × (active+new)); with the
    # low cand_thresh a bulk backfill window (e.g. 1500 re-queued orphan clusters) explodes
    # to ~180k candidate pairs -> 180k judge calls -> the worker "hangs". Live incremental
    # runs add ~tens of clusters/day, so this never bit them. Process the OLDEST N here;
    # the rest stay `classified` and the next tick picks them up (its anchors are the events
    # we just created at the boundary, so cross-window merges still happen).
    MAX_NEW_CLUSTERS = 120
    if len(new_clusters) > MAX_NEW_CLUSTERS:
        new_clusters = new_clusters[:MAX_NEW_CLUSTERS]

    # anchors: events whose date overlaps THIS tick's window (±win) — the only ones
    # a new cluster could merge into. The upper bound is essential: without it the
    # filter is `event_date >= oldest` (unbounded), so backlog re-dedup oldest-first
    # pulled EVERY event after `oldest` as an anchor (measured 3018 for one window) →
    # 936×3018 ≈ 2.8M fuzzy comparisons + thousands of judge calls per tick → the
    # worker hung. In live incremental runs oldest≈today so this was invisible.
    oldest = new_clusters[0]["dmin"] - win
    newest = max(c["dmax"] for c in new_clusters) + win
    recent_events = (Event.objects.filter(task=task,
                                          event_date__gte=oldest.date(),
                                          event_date__lte=newest.date())
                     .prefetch_related("posts__channel"))
    active = []
    for ev in recent_events:
        eposts = [p for p in ev.posts.all() if p.posted_at]
        if eposts:
            c = P._cluster_of(eposts)
            c["event"] = ev
            # ATTACH-AGE GUARD: anchor the event at its EARLIEST mention instead of
            # the rolling post span. _cluster_of() takes dmax from the latest attached
            # post, so every new repost extended dmax and kept the event eligible for
            # the next window — the snowball that grew 13k-post mega-events spanning
            # months. With a fixed anchor a post can only join an event whose first
            # mention is within `win` days — older events simply age out.
            anchor = min(p.posted_at for p in eposts)
            c["dmin"] = anchor
            c["dmax"] = anchor + win
            active.append(c)

    # Floors raised 35->50 and 45->60 (region branch below): with the old floors a
    # same-city pair («Махачкала» + 35-45% summary overlap) reached the judge, which
    # said ОДНА often enough to glue different incidents into one event.
    soft = max(50, task.dedup_cand_thresh - 20)
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
            elif _same_region(pool[a], pool[b]) and sf >= max(60, soft):
                pairs.append((a, b))          # same place + moderate similarity

    same = []
    logger.info("dedup pair-build: pool=%d new=%d -> pairs=%d forced=%d",
                len(pool), len(pool) - base, len(pairs), len(forced))
    if pairs:
        # memoize _judge_text per cluster: a cluster can appear in thousands of pairs
        # (cand_thresh is low), and _judge_text runs regex normalisation — recomputing it
        # per pair turned a dense backfill window into a multi-million regex hang.
        _jt = {i: P._judge_text(pool[i]["rep"]) for i in {x for pr in pairs for x in pr}}
        pairs_text = [(_jt[a], _jt[b]) for a, b in pairs]
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
                target = pool[anchor]["event"]
                # The recent_events queryset was prefetched at the start of this tick.
                # A concurrent review-worker may delete some of those Events as
                # "duplicate" verdicts at any moment — even between an exists() check
                # and the update below. So we run the attach in a savepoint and fall
                # back to creating a fresh Event if the FK constraint trips.
                try:
                    with transaction.atomic():
                        _attach_posts(target, new_posts)
                except (IntegrityError, DatabaseError):
                    # IntegrityError: FK trips because the target Event was deleted
                    # mid-attach. DatabaseError: same race, but caught later — the
                    # recompute save() matches 0 rows. Either way: fresh Event.
                    _create_event(task, new_posts)
                    created += 1
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


def recollect_incremental(task, date_from, date_to, job=None):
    """ADDITIVE re-collection: keep all existing posts+events, just re-run TeleZip collection
    for the period and add ONLY posts not already in the DB.

    The collector keys on (task, url) via `update_or_create`, so already-known posts are merely
    updated in place — they keep their stage and event linkage (stay `done`/attached) — while
    genuinely new urls are inserted at the default stage ('collected') and flow through the
    pipeline. dedup then attaches the new clusters to existing event anchors in-window or mints
    new events. We only clear the period's CollectChunks (collection bookkeeping; posts/events
    untouched) so `enqueue_collection` won't skip the ranges as 'already done'.
    Returns chunks_added."""
    CollectChunk.objects.filter(task=task, date_from__gte=date_from,
                                date_to__lte=date_to).delete()
    n_chunks = enqueue_collection(task, date_from, date_to, job=job)
    logger.info("recollect-incremental %s..%s: +%d chunks (additive, no wipe)",
                date_from, date_to, n_chunks)
    return n_chunks


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
