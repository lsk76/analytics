"""Простий інтерфейс /app/ (docs/simple-ui-design.md).

Лише читання: усі вʼюхи GET, майстер збору — макет. Логін і видимість — як в
адмінці (LOGIN_URL веде на її форму; задачі — services.access.visible_tasks).
"""
import json

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from analysis.models import TagCategory

from .services import charts, settings_drawer, status
from .services.access import approved_events, visible_tasks
from .services.periods import PRESETS, period_from_request


def _task(request, task_id):
    return get_object_or_404(visible_tasks(request.user), pk=task_id)


@login_required
def sections(request):
    now = timezone.now()
    rows = []
    for t in visible_tasks(request.user).order_by("-is_active", "id"):
        st = status.task_status(t, now)
        rows.append({"task": t, "status": st,
                     "subtitle": charts.headline(t.description, 90)})
    rows.sort(key=lambda r: (r["status"].state == status.STOPPED, -r["status"].week))
    return render(request, "simpleui/sections.html", {"rows": rows})


@login_required
def section(request, task_id):
    task = _task(request, task_id)
    period = period_from_request(request)
    ctx = {
        "task": task,
        "status": status.task_status(task),
        "period": period,
        "presets": PRESETS,
        "chart_json": json.dumps(charts.chart_data(task, period), ensure_ascii=False),
        "feed": charts.feed(task, period),
        "overview": settings_drawer.overview(task),
        "settings": settings_drawer.all_settings(task),
    }
    return render(request, "simpleui/section.html", ctx)


@login_required
def event(request, task_id, event_id):
    task = _task(request, task_id)
    e = (approved_events(task).select_related("region_subject")
         .prefetch_related("tags", "posts__source", "posts__channel")
         .filter(pk=event_id).first())
    if not e:
        raise Http404
    labels = dict(TagCategory.objects.values_list("key", "label"))
    themes = {}
    for tag in e.tags.all():
        if tag.category in charts.HIDDEN_TAG_CATEGORIES:
            continue
        themes.setdefault(labels.get(tag.category, tag.category), []).append(
            tag.name.replace("_", " "))
    links = [{"name": charts.source_name(p), "url": p.url, "date": p.posted_at}
             for p in e.posts.all()]
    return render(request, "simpleui/event.html", {
        "task": task, "event": e, "title": charts.headline(e.summary),
        "themes": sorted(themes.items()), "links": links,
        "back_query": period_from_request(request).query(),
    })


@login_required
def collect(request, task_id):
    task = _task(request, task_id)
    ov = settings_drawer.overview(task)
    presets = settings_drawer.collect_presets()
    chosen = request.GET.get("preset") or presets[0]["key"]
    days = next((p["days"] for p in presets if p["key"] == chosen), presets[0]["days"])
    return render(request, "simpleui/collect.html", {
        "task": task, "presets": presets, "chosen": chosen,
        "estimate": settings_drawer.collect_estimate(task, days, ov.n_sources),
        "overview": ov,
    })
