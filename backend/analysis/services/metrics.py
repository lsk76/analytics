"""
MetricSource — спільний аналітичний контракт для двох лінз даних:

  * EventSource — події-інциденти (Event; одиниця = дедуплікований інцидент);
  * PostSource  — monitor-критика (Post; одиниця = коментар-думка, БЕЗ дедупу:
                  метрика = «скільки людей критикує», злиття знищило б лічильник).

Обидва джерела віддають ОДНАКОВІ форми рядків для charts/admin/API:
  by_region()            → {id, name, count, reach, per_100k, reach_per_100k, population}
  republic_totals(ids)   → {id, name, population, count, per_100k}
  republic_timeseries(ids) → сирі рядки {bucket, region_id, count}
  by_tag(category, top)  → {id, name, count}
  timeseries()           → {bucket, count, reach, posts, channels}

Інваріанти (рішення unify-плану, 2026-06-19):
  * істина завжди COUNT() по predicate — жодної матеріалізації тут;
  * та сама формула per-100k від того самого Region.population в обох лінзах;
  * reach у PostSource = Σ підписників DISTINCT каналів зрізу (НЕ Sum по постах —
    канал із 10 коментарями рахувався б ×10);
  * PostSource виключає is_channel_repost (авто-форвард каналу ≠ думка людини);
  * people_count = COUNT(DISTINCT COALESCE(author_tg_id, content_hash)) — аноніми
    не зливаються в одного.
"""
from __future__ import annotations

from django.db.models import Count, Sum, TextField
from django.db.models.functions import (Cast, Coalesce, TruncDate, TruncMonth,
                                        TruncWeek)

from ..models import Region

TRUNC = {"day": TruncDate, "week": TruncWeek, "month": TruncMonth}


def _per_100k(count, population):
    return round(100000.0 * count / population, 2) if population else 0


def _populations(region_ids):
    return dict(Region.objects.filter(id__in=region_ids, population__isnull=False)
                .values_list("id", "population"))


class _BaseSource:
    """Спільна арифметика зрізів. Підкласи задають: date_field, як рахується
    reach/posts/channels. Поле регіону в обох моделях однойменне: region_subject."""

    date_field: str = ""

    def __init__(self, qs, gran: str = "day"):
        self.qs = qs.order_by()   # .order_by() ОБОВ'ЯЗКОВО: ordering changelist'а
        self.gran = gran          # інакше підмішується в GROUP BY (рядок на об'єкт)
        self.trunc = TRUNC[gran if gran in TRUNC else "day"]

    # ---- прості лічильники -------------------------------------------------
    def count(self) -> int:
        return self.qs.distinct().count()

    # ---- розбивка по суб'єктах РФ (та сама per-100k формула для обох лінз) --
    def by_region(self):
        raw = list(
            self.qs.exclude(region_subject__isnull=True)
                .values("region_subject_id", "region_subject__name")
                .annotate(count=Count("id", distinct=True))
                .order_by("-count")
        )
        pops = _populations([r["region_subject_id"] for r in raw])
        reach = self._reach_by_region([r["region_subject_id"] for r in raw])
        rows = []
        for r in raw:
            rid, name = r["region_subject_id"], r["region_subject__name"]
            pop, reach_v = pops.get(rid), int(reach.get(rid, 0))
            rows.append({
                "id": rid, "name": name, "count": r["count"], "reach": reach_v,
                "per_100k": _per_100k(r["count"], pop),
                "reach_per_100k": int(100000.0 * reach_v / pop) if pop else 0,
                "population": pop,
            })
        return rows

    def republic_totals(self, rep_ids):
        republics = list(Region.objects.filter(id__in=rep_ids, population__isnull=False)
                         .order_by("name").values("id", "name", "population"))
        totals = {r["region_subject_id"]: r["n"] for r in
                  self.qs.filter(region_subject_id__in=rep_ids)
                      .values("region_subject_id")
                      .annotate(n=Count("id", distinct=True))}
        return [{
            "id": r["id"], "name": r["name"], "population": r["population"],
            "count": totals.get(r["id"], 0),
            "per_100k": _per_100k(totals.get(r["id"], 0), r["population"]),
        } for r in republics]

    def republic_timeseries(self, rep_ids):
        return list(
            self.qs.filter(region_subject_id__in=rep_ids)
                .exclude(**{f"{self.date_field}__isnull": True})
                .annotate(bucket=self.trunc(self.date_field))
                .values("bucket", "region_subject_id")
                .annotate(count=Count("id", distinct=True))
                .order_by("bucket")
        )

    # ---- розбивка по тегах ---------------------------------------------------
    def by_tag(self, category: str, top: int = 20):
        return [{"id": r["tags__id"], "name": r["tags__name"], "count": r["count"]}
                for r in self.qs.filter(tags__category=category)
                    .values("tags__id", "tags__name")
                    .annotate(count=Count("id", distinct=True))
                    .order_by("-count")[:top]]

    # ---- перевизначається підкласами ----------------------------------------
    def _reach_by_region(self, region_ids) -> dict:
        raise NotImplementedError

    def timeseries(self):
        raise NotImplementedError


class EventSource(_BaseSource):
    """Лінза подій: одиниця = Event (дедуплікований інцидент). reach —
    предрахований на Event (Σ підписників унікальних каналів події)."""

    date_field = "event_date"

    def _reach_by_region(self, region_ids):
        return {r["region_subject_id"]: r["reach"] or 0 for r in
                self.qs.exclude(region_subject__isnull=True)
                    .values("region_subject_id").annotate(reach=Sum("reach"))}

    def timeseries(self):
        return list(
            self.qs.exclude(event_date__isnull=True)
                .annotate(bucket=self.trunc("event_date"))
                .values("bucket")
                .annotate(count=Count("id", distinct=True),
                          reach=Sum("reach"), posts=Sum("post_count"))
                .order_by("bucket")
        )


class PostSource(_BaseSource):
    """Лінза критики: одиниця = коментар (Post, is_relevant=True). Без дедупу.
    Краї: виключаємо авто-репости каналу; людей рахуємо по author_tg_id із
    fallback на content_hash (анонім ≠ анонім)."""

    date_field = "posted_at"

    def __init__(self, qs, gran: str = "day"):
        super().__init__(
            qs.filter(is_relevant=True).exclude(is_channel_repost=True), gran)

    _AUTHOR_KEY = Coalesce(Cast("author_tg_id", TextField()), "content_hash",
                           output_field=TextField())

    def people_count(self) -> int:
        """Скільки РІЗНИХ людей (не коментарів) у зрізі — окрема метрика поряд
        із count(); Σ по target-зрізах може перевищувати її (мульти-таргет)."""
        return (self.qs.annotate(_author=self._AUTHOR_KEY)
                .values("_author").distinct().count())

    def _reach_by_region(self, region_ids):
        # Σ підписників DISTINCT каналів зрізу — пастка R4 закрита:
        # дистинкт-пари (регіон, канал) агрегуються в python (каналів сотні).
        pairs = (self.qs.exclude(region_subject__isnull=True)
                 .exclude(channel__isnull=True)
                 .values_list("region_subject_id", "channel_id",
                              "channel__subscribers").distinct())
        out: dict = {}
        for rid, _ch, subs in pairs:
            out[rid] = out.get(rid, 0) + (subs or 0)
        return out

    def timeseries(self):
        rows = list(
            self.qs.exclude(posted_at__isnull=True)
                .annotate(bucket=self.trunc("posted_at"))
                .values("bucket")
                .annotate(count=Count("id", distinct=True),
                          channels=Count("channel", distinct=True))
                .order_by("bucket")
        )
        for r in rows:   # симетрія форми з EventSource
            r["posts"], r["reach"] = r["count"], 0
        return rows
