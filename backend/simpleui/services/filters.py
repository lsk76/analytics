"""Фільтри секції — ті самі, що в адмінці подій (EventAdmin.get_list_filter),
з тими самими іменами параметрів URL, тож посилання взаємозамінні:

  region_id=<id>…            суб'єкт РФ (кілька = АБО)          — SubjectFilter
  tag_<cat>=<id>…            тег включити (у межах категорії АБО) — TagCategoryMultiSelectFilter
  tag_<cat>_excl=<id>…       тег виключити
  channel_id=<id>…           канал Telegram                      — ChannelFilter
  source_id=<id>…            джерело інформпростору (Source)
  channel_count__gte=<n>     мінімум каналів/джерел               — ChannelCountFilter
  q=<текст>                  пошук по опису/населеному пункту     — search_fields

Не переносимо свідомо: статус аудиту й задача (зафіксовані), охоплення (reach
не показуємо), спецфільтри задачі етносутичок (InterEthnic/RosMinority).
Фасетні лічильники рахуються на зрізі з усіма ІНШИМИ фільтрами — як в адмінці.
"""
from dataclasses import dataclass, field

from django.db.models import Count, Q

from analysis.models import Channel, Region, Source, Tag, TagCategory

TAG_PREFIX = "tag_"
EXCL_SUFFIX = "_excl"
FACET_LIMIT = 40          # тегів/джерел у фасеті, більше — не читається


def _ints(values):
    return [int(v) for v in values if str(v).isdigit()]


@dataclass
class Filters:
    regions: list = field(default_factory=list)
    tags_inc: dict = field(default_factory=dict)     # cat → [tag_id]
    tags_exc: dict = field(default_factory=dict)
    channels: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    min_channels: int = 0
    q: str = ""

    @classmethod
    def from_request(cls, request):
        g = request.GET
        f = cls(regions=_ints(g.getlist("region_id")),
                channels=_ints(g.getlist("channel_id")),
                sources=_ints(g.getlist("source_id")),
                q=(g.get("q") or "").strip()[:200])
        try:
            f.min_channels = max(0, int(g.get("channel_count__gte") or 0))
        except ValueError:
            f.min_channels = 0
        for key in g.keys():
            if not key.startswith(TAG_PREFIX):
                continue
            cat = key[len(TAG_PREFIX):]
            if cat.endswith(EXCL_SUFFIX):
                f.tags_exc[cat[:-len(EXCL_SUFFIX)]] = _ints(g.getlist(key))
            else:
                f.tags_inc[cat] = _ints(g.getlist(key))
        return f

    @property
    def active(self) -> bool:
        return bool(self.regions or self.channels or self.sources or self.min_channels
                    or self.q or any(self.tags_inc.values()) or any(self.tags_exc.values()))

    def apply(self, qs, skip=None):
        """Накласти всі фільтри, крім `skip` (для фасетних лічильників)."""
        if self.regions and skip != "region":
            qs = qs.filter(region_subject_id__in=self.regions)
        for cat, ids in self.tags_inc.items():
            if ids and skip != f"tag:{cat}":
                qs = qs.filter(tags__id__in=ids).distinct()
        for cat, ids in self.tags_exc.items():
            if ids and skip != f"tag:{cat}":
                qs = qs.exclude(tags__id__in=ids)
        if self.channels and skip != "source":
            qs = qs.filter(posts__channel_id__in=self.channels).distinct()
        if self.sources and skip != "source":
            qs = qs.filter(posts__source_id__in=self.sources).distinct()
        if self.min_channels:
            qs = qs.filter(channel_count__gte=self.min_channels)
        if self.q:
            qs = qs.filter(Q(summary__icontains=self.q) | Q(settlement__icontains=self.q))
        return qs

    # ---- активні фільтри людською мовою (чіпи над стрічкою) -----------------
    def chips(self):
        out = []
        if self.regions:
            names = Region.objects.filter(id__in=self.regions).values_list("name", flat=True)
            out.append(("Регіон", ", ".join(names)))
        labels = dict(TagCategory.objects.values_list("key", "label"))
        for prefix, groups in (("", self.tags_inc), ("без: ", self.tags_exc)):
            for cat, ids in groups.items():
                names = list(Tag.objects.filter(id__in=ids).values_list("name", flat=True))
                if names:
                    out.append((prefix + labels.get(cat, cat),
                                ", ".join(n.replace("_", " ") for n in names)))
        if self.channels:
            names = [c.title or c.username for c in Channel.objects.filter(id__in=self.channels)]
            out.append(("Джерело", ", ".join(names)))
        if self.sources:
            names = Source.objects.filter(id__in=self.sources).values_list("name", flat=True)
            out.append(("Джерело", ", ".join(names)))
        if self.min_channels:
            out.append(("Джерел не менше", str(self.min_channels)))
        if self.q:
            out.append(("Пошук", self.q))
        return out


def _keep_visible(present, selected, model, name_field="name"):
    """Обрані значення лишаються у фасеті, навіть якщо зараз дають 0 — інакше
    їх не зняти (той самий прийом, що в адмінці)."""
    for pk in selected:
        if pk not in present:
            obj = model.objects.filter(pk=pk).first()
            if obj:
                present[pk] = (getattr(obj, name_field) or str(obj), 0)


def facets(base_qs, f: Filters):
    """Опції фільтрів з лічильниками. base_qs — події задачі за період."""
    # регіони
    rows = (f.apply(base_qs, skip="region").order_by()
            .exclude(region_subject__isnull=True)
            .values("region_subject_id", "region_subject__name")
            .annotate(n=Count("id", distinct=True)).order_by("-n"))
    present = {r["region_subject_id"]: (r["region_subject__name"], r["n"]) for r in rows}
    _keep_visible(present, f.regions, Region)
    regions = [{"id": k, "name": v[0], "n": v[1], "on": k in f.regions}
               for k, v in sorted(present.items(), key=lambda kv: (-kv[1][1], kv[1][0]))]

    # теги — по категоріях, лише ті, що є у зрізі (порядок як у реєстрі)
    cats = []
    cat_order = {c.key: (c.order, c.label) for c in TagCategory.objects.all()}
    cat_keys = set(base_qs.order_by().filter(tags__isnull=False)
                   .values_list("tags__category", flat=True).distinct())
    cat_keys |= {c for c, ids in f.tags_inc.items() if ids} | {c for c, ids in f.tags_exc.items() if ids}
    for cat in sorted(cat_keys, key=lambda k: cat_order.get(k, (999, k))):
        inc, exc = f.tags_inc.get(cat, []), f.tags_exc.get(cat, [])
        rows = (f.apply(base_qs, skip=f"tag:{cat}").order_by()
                .filter(tags__category=cat).values("tags__id", "tags__name")
                .annotate(n=Count("id", distinct=True)).order_by("-n")[:FACET_LIMIT])
        present = {r["tags__id"]: (r["tags__name"], r["n"]) for r in rows}
        _keep_visible(present, inc + exc, Tag)
        cats.append({
            "key": cat, "label": cat_order.get(cat, (0, cat))[1],
            "active": bool(inc or exc),
            "items": [{"id": k, "name": v[0].replace("_", " "), "n": v[1],
                       "inc": k in inc, "exc": k in exc}
                      for k, v in sorted(present.items(), key=lambda kv: (-kv[1][1], kv[1][0]))],
        })

    # джерела: Source (інформпростір) або канал Telegram
    src_qs = f.apply(base_qs, skip="source")
    rows = (src_qs.order_by().filter(posts__source__isnull=False)
            .values("posts__source_id", "posts__source__name")
            .annotate(n=Count("id", distinct=True)).order_by("-n")[:FACET_LIMIT])
    present = {r["posts__source_id"]: (r["posts__source__name"], r["n"]) for r in rows}
    _keep_visible(present, f.sources, Source)
    sources = [{"id": k, "name": v[0], "n": v[1], "on": k in f.sources, "param": "source_id"}
               for k, v in present.items()]
    if not sources:
        rows = (src_qs.order_by().filter(posts__channel__isnull=False)
                .values("posts__channel_id", "posts__channel__title", "posts__channel__username")
                .annotate(n=Count("id", distinct=True)).order_by("-n")[:FACET_LIMIT])
        present = {r["posts__channel_id"]: (r["posts__channel__title"] or "@" + (r["posts__channel__username"] or "?"), r["n"])
                   for r in rows}
        _keep_visible(present, f.channels, Channel, "title")
        sources = [{"id": k, "name": v[0], "n": v[1], "on": k in f.channels, "param": "channel_id"}
                   for k, v in present.items()]
    sources.sort(key=lambda s: (-s["n"], s["name"]))

    return {"regions": regions, "tag_cats": cats, "sources": sources}
