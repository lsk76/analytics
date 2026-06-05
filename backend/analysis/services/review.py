"""
Final AI audit stage — a stronger (pricier) model reviews each finalized Event and
does the "manual" QA pass: drop false positives (films / historical cases / absurd
matches), merge missed duplicates, fix geo, and (full audit) rewrite summary + tags.

Runs AFTER dedup, on Events (not posts). An Event is reviewable once it is past the
dedup window (it can no longer gain new posts). Verdicts are applied autonomously and
destructively per the task config: drop => delete, duplicate => merge into the target.

    worker:  python manage.py run_worker --stage review
"""
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone as djtz

from analysis.models import Event, Post, Tag
from analysis.services import llm
from analysis.services.normalize import resolve_region, resolve_in_category
from analysis.services.stages import _ready_through, _attach_posts, LOCK_TIMEOUT

logger = logging.getLogger(__name__)

REVIEW_BATCH = 6          # events claimed per pass
CAND_PER_EVENT = 10       # duplicate candidates shown to the model (region/tag-filtered)
SRC_POSTS = 1             # source posts shown per event (reposts are ~identical; 1 is enough)

_DEFAULT_REVIEW_SYS = (
    "Ти — суворий редактор-аудитор подій. Тобі дають ОДНУ подію (короткий опис, гео, "
    "теги) разом з ОРИГІНАЛЬНИМИ текстами постів-джерел і список сусідніх подій-кандидатів "
    "на дубль. Твоє завдання — вирішити долю події СТРОГО за джерелами:\n"
    "- verdict='drop', якщо це НЕ реальний інцидент потрібної теми (новина про кіно/культуру, "
    "адміністративна довідка, історична справа давніх років, абсурдний/нерелевантний матч, "
    "подія ПОЗА потрібною територією).\n"
    "- verdict='duplicate' + duplicate_of=<id з кандидатів>, якщо це той самий реальний "
    "інцидент, що й одна з сусідніх подій.\n"
    "- verdict='keep' інакше. Тоді за потреби ВИПРАВ поля під джерела: region (суб'єкт без "
    "міста), settlement (місто), summary (1 точне речення), tags (лише за наявними категоріями).\n"
    "Суди ЛИШЕ за текстами джерел; опис міг бути неточним."
)


def _settle_to(task):
    ready = _ready_through(
        task, [Post.STAGE_COLLECTED, Post.STAGE_ENRICHED, Post.STAGE_PRECLUSTERED,
               Post.STAGE_CLASSIFIED, Post.STAGE_DEDUPED])
    return (ready - timedelta(days=task.dedup_window_days)) if ready else None


def _claim_events(task, limit):
    cutoff = djtz.now() - LOCK_TIMEOUT
    settle_to = _settle_to(task)
    with transaction.atomic():
        qs = (Event.objects.select_for_update(skip_locked=True)
              .filter(task=task, review_status=Event.REVIEW_PENDING)
              .filter(models_q_unlocked(cutoff)))
        if settle_to is not None:
            qs = qs.filter(event_date__lte=settle_to)
        ids = list(qs.order_by("event_date", "id").values_list("id", flat=True)[:limit])
        if ids:
            Event.objects.filter(id__in=ids).update(review_locked_at=djtz.now())
    return ids


def models_q_unlocked(cutoff):
    from django.db.models import Q
    return Q(review_locked_at__isnull=True) | Q(review_locked_at__lt=cutoff)


def _tags_by_category(event, task):
    cats = {c.key for c in task.tag_categories.all()}
    out = {}
    for t in event.tags.all():
        if t.category in cats:
            out.setdefault(t.category, []).append(t.name)
    return out


def _candidates(task, event):
    """Duplicate candidates = ALREADY-AUDITED (approved) events in the ±window that SHARE
    the RF subject or at least one tag, ranked by relevance (same region + shared-tag
    count). Restricting to approved events means each pair is judged ONCE: when the
    later-processed event is reviewed it looks back at the already-vetted one, so we never
    ask "A vs B" and "B vs A" separately. Merges always fold into the vetted event."""
    win = timedelta(days=task.dedup_window_days)
    my_tags = {t.id for t in event.tags.all()}
    pool = (Event.objects.filter(task=task,
                                 event_date__gte=event.event_date - win,
                                 event_date__lte=event.event_date + win,
                                 review_status=Event.REVIEW_APPROVED)
            .exclude(id=event.id)
            .select_related("region_subject").prefetch_related("tags"))
    scored = []
    for e in pool:
        same_region = bool(event.region_subject_id) and e.region_subject_id == event.region_subject_id
        shared = len(my_tags & {t.id for t in e.tags.all()})
        if not same_region and not shared:
            continue                                   # unrelated — don't show the model
        score = (10 if same_region else 0) + shared    # region dominates, tags break ties
        scored.append((score, e.event_date, e))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"id": e.id, "date": str(e.event_date),
             "region": e.region_subject.name if e.region_subject else (e.region or ""),
             "shared_tags": [t.name for t in e.tags.all() if t.id in my_tags],
             "summary": (e.summary or "")[:160]}
            for _, _, e in scored[:CAND_PER_EVENT]]


def _build_user_msg(task, event, cand):
    posts = list(event.posts.order_by("posted_at")[:SRC_POSTS])
    sources = [{"text": (p.text or "")[:900]} for p in posts]
    payload = {
        "event": {
            "id": event.id, "date": str(event.event_date),
            "region": event.region_subject.name if event.region_subject else "",
            "settlement": event.settlement,
            "summary": event.summary,
            "tags": _tags_by_category(event, task),
        },
        "sources": sources,
        "duplicate_candidates": cand,
        "tag_categories": [c.key for c in task.tag_categories.all()],
    }
    schema = ('Поверни СТРОГО JSON: {"verdict":"keep|drop|duplicate",'
              '"duplicate_of":<id або null>,"region":"<суб\'єкт або порожньо>",'
              '"settlement":"<місто або порожньо>","summary":"<1 речення>",'
              '"tags":{"<категорія>":["..."]},"reason":"<коротко чому>"}')
    return json.dumps(payload, ensure_ascii=False) + "\n\n" + schema


def _apply(task, event, verdict, cand_ids):
    """Apply the audit verdict destructively. Returns a short action label."""
    v = (verdict.get("verdict") or "keep").lower()
    reason = (verdict.get("reason") or "")[:500]

    if v == "drop":
        Post.objects.filter(event=event).update(event=None)
        logger.info("review drop #%s: %s", event.id, reason)
        event.delete()
        return "drop"

    if v == "duplicate":
        tgt_id = verdict.get("duplicate_of")
        if tgt_id in cand_ids:
            target = Event.objects.filter(id=tgt_id).first()
            if target:
                moved = list(Post.objects.filter(event=event))
                _attach_posts(target, moved)        # recomputes count/reach
                logger.info("review merge #%s -> #%s: %s", event.id, tgt_id, reason)
                event.delete()
                return f"merge->#{tgt_id}"
        # bad/unknown target -> fall through to keep

    # keep: optionally fix geo / summary / tags, then approve
    fields = []
    summ = (verdict.get("summary") or "").strip()
    if summ and summ != event.summary:
        event.summary = summ[:1000]; fields.append("summary")
    if task.geo_enabled:
        region = (verdict.get("region") or "").strip()
        sett = (verdict.get("settlement") or "").strip()
        loc = ", ".join(x for x in (sett, region) if x)
        if loc:
            rsubj, rsett = resolve_region(loc)
            if rsubj and rsubj != event.region_subject:
                event.region_subject = rsubj; fields.append("region_subject")
            if rsett and rsett != event.settlement:
                event.settlement = rsett[:160]; fields.append("settlement")
            if region and region != event.region:
                event.region = region[:128]; fields.append("region")
    new_tags = verdict.get("tags") or {}
    if isinstance(new_tags, dict) and new_tags:
        objs = []
        for c in task.tag_categories.all():
            vals = new_tags.get(c.key) or []
            if isinstance(vals, str):
                vals = [vals]
            for val in vals:
                if val and (o := resolve_in_category(str(val), c.key, c.closed)):
                    objs.append(o)
        if objs:
            event.tags.set(objs)
    event.review_status = Event.REVIEW_APPROVED
    event.review_notes = reason
    event.reviewed_at = djtz.now()
    event.review_locked_at = None
    event.save(update_fields=["summary", "region", "region_subject", "settlement",
                              "review_status", "review_notes", "reviewed_at",
                              "review_locked_at"])
    return "keep" + (f" (fix:{','.join(fields)})" if fields else "")


def review_once(task):
    """Audit one batch of pending events with the task's pricier model. Returns True if
    it did work."""
    if not task.review_enabled:
        return False
    ids = _claim_events(task, REVIEW_BATCH)
    if not ids:
        return False
    model = task.review_model or "anthropic/claude-sonnet-4"
    system = task.review_prompt.strip() or _DEFAULT_REVIEW_SYS

    actions = []
    for eid in ids:
        event = (Event.objects.filter(id=eid)
                 .select_related("region_subject").prefetch_related("tags", "posts").first())
        if not event:
            continue
        cand = _candidates(task, event)
        cand_ids = {c["id"] for c in cand}
        user = _build_user_msg(task, event, cand)
        raw = _ask(system, user, model)
        verdict = llm.extract_json(raw) or {}
        if isinstance(verdict, list):
            verdict = verdict[0] if verdict else {}
        try:
            actions.append(_apply(task, event, verdict, cand_ids))
        except Exception as e:  # noqa: BLE001 — never let one event kill the batch
            logger.warning("review apply #%s failed: %s", eid, e)
            Event.objects.filter(id=eid).update(review_locked_at=None)
    logger.info("review: %d events -> %s", len(ids), ", ".join(actions))
    return True


def _ask(system, user, model):
    import asyncio
    async def go():
        client = llm.make_client()
        try:
            return await llm.query(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}], model=model, client=client)
        finally:
            await client.close()
    return asyncio.run(go())
