"""Навігація адмінки «для людей»: верхній навбар (Дослідження · Канали · Акаунти)
і панель дослідження з вкладками (Події · Графіки · Налаштування · Збори/Джерела/Чати).

Активна вкладка визначається лише за адресою й параметрами запиту — ніякого
стану в сесії. Дослідження береться з ?task= / ?task__id__exact= або з адреси
форми задачі; чуже дослідження (owner) панель не показує.
"""
import re

from django import template

from analysis.models import AnalysisTask

register = template.Library()

_TASK_PATH = re.compile(r"^/admin/analysis/analysistask/(\d+)/")


def _current_task(request):
    g = request.GET
    tid = g.get("task") or g.get("task__id__exact") or g.get("task__id")
    if not tid:
        m = _TASK_PATH.match(request.path)
        tid = m.group(1) if m else None
    if not tid or not str(tid).isdigit():
        return None
    qs = AnalysisTask.objects.filter(pk=int(tid))
    if not request.user.is_superuser:
        qs = qs.filter(owner=request.user)
    return qs.first()


@register.inclusion_tag("admin/studynav/topnav.html", takes_context=True)
def study_topnav(context):
    request = context["request"]
    p = request.path
    user = request.user
    tabs = [{"label": "Дослідження", "url": "/admin/",
             "active": p == "/admin/" or p.startswith("/admin/analysis/") and not p.startswith("/admin/analysis/channel/")}]
    if user.has_perm("analysis.view_channel"):
        tabs.append({"label": "Канали", "url": "/admin/analysis/channel/",
                     "active": p.startswith("/admin/analysis/channel/")})
    if user.has_perm("accounts.view_telegramaccount"):
        tabs.append({"label": "Акаунти", "url": "/admin/accounts/telegramaccount/",
                     "active": p.startswith("/admin/accounts/")})
    return {"tabs": tabs, "is_superuser": user.is_superuser}


@register.inclusion_tag("admin/studynav/studybar.html", takes_context=True)
def study_bar(context):
    request = context["request"]
    task = _current_task(request)
    if not task:
        return {"task": None}
    p = request.path
    perm = request.user.has_perm
    tabs = []
    for link in study_links(task):
        if perm(link["perm"]):
            tabs.append({**link, "active": bool(link["prefix"]) and p.startswith(link["prefix"])})
    collect_url = ""
    if task.pipeline not in (AnalysisTask.PIPELINE_INFOSPACE, AnalysisTask.PIPELINE_TGSEARCH) \
            and perm("analysis.add_researchrun"):
        collect_url = f"/admin/analysis/researchrun/add/?task={task.id}"
    return {"task": task, "tabs": tabs, "collect_url": collect_url}


def study_links(task):
    """Вкладки дослідження: (назва, адреса, префікс для «активна», потрібне право).
    Одне джерело правди для панелі дослідження і карток на стартовій."""
    tid = task.id
    links = [
        {"label": "Події", "url": f"/admin/analysis/event/?task={tid}",
         "prefix": "/admin/analysis/event/", "perm": "analysis.view_event"},
        {"label": "Графіки", "url": f"/admin/analysis/event/?task={tid}#charts",
         "prefix": "", "perm": "analysis.view_event"},
        {"label": "Налаштування", "url": f"/admin/analysis/analysistask/{tid}/change/",
         "prefix": "/admin/analysis/analysistask/", "perm": "analysis.view_analysistask"},
    ]
    if task.pipeline == AnalysisTask.PIPELINE_INFOSPACE:
        links.append({"label": "Джерела", "url": f"/admin/analysis/sourcesubscription/?task__id__exact={tid}",
                      "prefix": "/admin/analysis/sourcesubscription/", "perm": "analysis.view_sourcesubscription"})
    else:
        if task.pipeline in (AnalysisTask.PIPELINE_MONITOR, AnalysisTask.PIPELINE_TGSEARCH,
                             AnalysisTask.PIPELINE_RESEARCH):
            links.append({"label": "Чати", "url": f"/admin/analysis/monitorchat/?task__id__exact={tid}",
                          "prefix": "/admin/analysis/monitorchat/", "perm": "analysis.view_monitorchat"})
        if task.pipeline != AnalysisTask.PIPELINE_TGSEARCH:
            links.append({"label": "Збори", "url": f"/admin/analysis/researchrun/?task__id__exact={tid}",
                          "prefix": "/admin/analysis/researchrun/", "perm": "analysis.view_researchrun"})
    return links
