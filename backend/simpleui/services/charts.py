"""Дані для чотирьох графіків секції та стрічки подій.

Уся арифметика — через analysis.services.metrics.EventSource (той самий контракт,
що в адмінці), тож цифри тут збігаються з `#charts` у списку подій. Лише
«Хто пише найбільше» рахується напряму по Post — це довідка про джерела, а не
аналітична метрика.
"""
from datetime import timedelta

from django.db.models import Count, F
from django.db.models.functions import Coalesce

from analysis.models import Post, TagCategory
from analysis.services.metrics import EventSource

from .access import approved_events

TOP_N = 10
FEED_N = 30
HIDDEN_TAG_CATEGORIES = {"importance"}   # службова вісь, не тема для читача


def period_events(task, period):
    return approved_events(task).filter(event_date__range=(period.date_from, period.date_to))


def _fill_buckets(rows, period):
    """Кожен день/тиждень періоду присутній на осі, навіть якщо подій 0."""
    by = {r["bucket"]: r["count"] for r in rows}
    step = timedelta(days=1 if period.gran == "day" else 7)
    cur = period.date_from
    if period.gran == "week":
        cur = cur - timedelta(days=cur.weekday())   # TruncWeek → понеділок
    out = []
    while cur <= period.date_to:
        out.append({"date": cur.isoformat(), "count": by.get(cur, 0)})
        cur += step
    return out


def main_tag_category(task, qs):
    """Категорія для «Про що»: перша з категорій задачі (крім службових),
    інакше — та, у якої найбільше подій у зрізі."""
    for cat in task.tag_categories.order_by("order", "key"):
        if cat.key not in HIDDEN_TAG_CATEGORIES:
            return cat
    row = (qs.order_by().filter(tags__isnull=False)
           .exclude(tags__category__in=HIDDEN_TAG_CATEGORIES)
           .values("tags__category").annotate(n=Count("id")).order_by("-n").first())
    return TagCategory.objects.filter(key=row["tags__category"]).first() if row else None


def top_sources(qs):
    """Джерела подій зрізу: Source (infospace) або канал Telegram."""
    rows = (Post.objects.filter(event__in=qs).order_by()
            .annotate(src=Coalesce(F("source__name"), F("channel__title"),
                                   F("channel_name")))
            .values("src").annotate(count=Count("event", distinct=True))
            .order_by("-count")[:TOP_N])
    return [{"name": r["src"] or "без назви", "count": r["count"]} for r in rows]


def chart_data(task, period):
    qs = period_events(task, period)
    src = EventSource(qs, period.gran)
    cat = main_tag_category(task, qs)
    regions = src.by_region()[:TOP_N]
    return {
        "gran": period.gran,
        "timeseries": _fill_buckets(src.timeseries(), period),
        "regions": [{"name": r["name"], "count": r["count"]} for r in regions],
        "tags": ([{"name": t["name"].replace("_", " "), "count": t["count"]}
                  for t in src.by_tag(cat.key, TOP_N)] if cat else []),
        "tags_label": cat.label if cat else "Теми",
        "sources": top_sources(qs),
        "total": src.count(),
    }


def headline(summary: str, limit: int = 140) -> str:
    """Заголовок події = перше речення опису (обрізане)."""
    s = " ".join((summary or "").split())
    for sep in (". ", "! ", "? "):
        i = s.find(sep)
        if 0 < i < limit:
            return s[:i + 1]
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def feed(task, period, limit=FEED_N):
    qs = (period_events(task, period)
          .select_related("region_subject")
          .prefetch_related("posts__source", "posts__channel")
          .order_by("-event_date", "-created_at")[:limit])
    items = []
    for e in qs:
        p = next(iter(e.posts.all()), None)
        items.append({
            "id": e.id, "date": e.event_date,
            "title": headline(e.summary),
            "region": e.region_subject.name if e.region_subject else "",
            "source": source_name(p),
        })
    return items


def source_name(post) -> str:
    if not post:
        return ""
    if post.source_id and post.source:
        return post.source.name
    if post.channel_id and post.channel:
        return post.channel.title or ("@" + post.channel.username if post.channel.username else "")
    return post.channel_name or ""
