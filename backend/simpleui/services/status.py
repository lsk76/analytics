"""Світлофор стану секції: працює / є проблема / зупинено.

Джерело — лише існуючі дані: Source (health infospace), ResearchRun+CollectChunk
(збори TeleZip), MonitorChat (стрім tgsearch), Post.stage (черга обробки),
Event (нове за сьогодні/тиждень). Внутрішні стани перекладаються в людські
фрази (docs/simple-ui-design.md §3.2); технічні слова назовні не виходять.
"""
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from analysis.models import (AnalysisTask, CollectChunk, MonitorChat, Post,
                             ResearchRun, Source)

from .access import approved_events

LIVE, PROBLEM, STOPPED = "live", "problem", "stopped"
STATE_LABEL = {LIVE: "Працює", PROBLEM: "Є проблема", STOPPED: "Зупинено"}

# Пости, що ще в роботі (не done/failed) — «обробляємо зібране».
_NON_TERMINAL = [s for s, _ in Post.STAGE_CHOICES
                 if s not in (Post.STAGE_DONE, Post.STAGE_FAILED)]
# Стадії, де пости ЗА ДИЗАЙНОМ чекають людину/агента (гібридне тегування,
# docs/comments-analysis-pipeline.md): це не збій, а «зібране чекає обробки».
_WAITING_STAGES = {Post.STAGE_MON_PRESCREENED}

# Скільки годин без оновлення — вже проблема для конвеєрів, що працюють самі.
_SILENT_HOURS = {AnalysisTask.PIPELINE_INFOSPACE: 3, AnalysisTask.PIPELINE_TGSEARCH: 24}
_STUCK_QUEUE_HOURS = 6


def ago(dt, now=None) -> str:
    """«5 хв тому», «3 год тому», «2 дн. тому» — без секунд і таймзон."""
    if not dt:
        return "ще не було"
    now = now or timezone.now()
    s = int((now - dt).total_seconds())
    if s < 90:
        return "щойно"
    if s < 3600:
        return f"{s // 60} хв тому"
    if s < 86400:
        return f"{s // 3600} год тому"
    return f"{s // 86400} дн. тому"


@dataclass
class Status:
    state: str
    note: str = ""                # одна людська фраза під станом
    last_update: object = None    # datetime останнього збору/оновлення
    today: int = 0
    week: int = 0
    activity: str = ""            # «збираємо зараз», «обробляємо зібране»
    details: list = field(default_factory=list)   # м'які зауваження (не проблеми)

    @property
    def label(self) -> str:
        return STATE_LABEL[self.state]

    @property
    def last_update_text(self) -> str:
        return ago(self.last_update)


def _counts(task, now):
    ev = approved_events(task)
    today = now.date()
    return (ev.filter(event_date=today).count(),
            ev.filter(event_date__gte=today - timedelta(days=6)).count())


def _queue(task, now):
    """(постів у роботі воркерів, найстаріший, постів що чекають агента)."""
    auto = [s for s in _NON_TERMINAL if s not in _WAITING_STAGES]
    q = (Post.objects.filter(task=task, stage__in=auto).order_by()
         .aggregate(n=Count("id"), oldest=Min("created_at")))
    waiting = Post.objects.filter(task=task, stage__in=_WAITING_STAGES).count()
    return q["n"] or 0, q["oldest"], waiting


def _infospace(task, now, st: Status):
    srcs = Source.objects.filter(subscriptions__task=task, subscriptions__is_active=True,
                                 is_active=True).distinct()
    total = srcs.count()
    st.last_update = srcs.aggregate(m=Max("last_ok_at"))["m"]
    failing = srcs.filter(Q(consecutive_failures__gte=3) | Q(quality_ok=False)).count()
    silent = srcs.filter(Q(last_ok_at__lt=now - timedelta(hours=24))
                         | Q(last_ok_at__isnull=True)).count()
    if total == 0:
        st.state, st.note = PROBLEM, "немає жодного джерела"
        return
    if not st.last_update or st.last_update < now - timedelta(hours=_SILENT_HOURS[task.pipeline]):
        st.state, st.note = PROBLEM, f"дані не оновлювались {ago(st.last_update, now)}"
        return
    if failing * 10 > total:      # більше десятої частини джерел не відповідає
        st.state, st.note = PROBLEM, f"{failing} із {total} джерел не відповідають"
        return
    bad = failing + silent
    if bad:
        st.details.append(f"{bad} із {total} джерел мовчать — решта працює")
    st.note = f"оновлено {ago(st.last_update, now)}"


def _tgsearch(task, now, st: Status):
    chats = MonitorChat.objects.filter(task=task, is_active=True)
    agg = chats.aggregate(s=Max("last_streamed_at"), q=Max("last_searched_at"))
    st.last_update = max((d for d in (agg["s"], agg["q"]) if d), default=None)
    if not chats.exists():
        st.state, st.note = PROBLEM, "немає жодного чату"
        return
    if not st.last_update or st.last_update < now - timedelta(hours=_SILENT_HOURS[task.pipeline]):
        st.state, st.note = PROBLEM, f"чати не читались {ago(st.last_update, now)}"
        return
    st.note = f"оновлено {ago(st.last_update, now)}"


def _run_based(task, now, st: Status):
    """events / monitor / research: збір за період на запит (ResearchRun)."""
    run = ResearchRun.objects.filter(task=task).order_by("-created_at").first()
    chunks = CollectChunk.objects.filter(task=task)
    last_chunk = chunks.filter(status="done").aggregate(m=Max("finished_at"))["m"]
    st.last_update = (run and run.finished_at) or last_chunk
    if not st.last_update:   # задачі, наповнені скриптами: свіжість = останній Event
        st.last_update = approved_events(task).aggregate(m=Max("created_at"))["m"]
    if run and run.status == "failed":
        st.state, st.note = PROBLEM, "останній збір не вдався"
        return
    if chunks.filter(status="failed").exists():
        st.state, st.note = PROBLEM, "частину періоду не вдалося зібрати"
        return
    if run and run.status in ("pending", "collecting"):
        st.activity = "збираємо зараз"
    elif run and run.status in ("collected", "awaiting_agent"):
        st.activity = "обробляємо зібране"
    st.note = f"останній збір {ago(st.last_update, now)}"


def task_status(task, now=None) -> Status:
    now = now or timezone.now()
    st = Status(state=LIVE)
    st.today, st.week = _counts(task, now)
    if not task.is_active:
        st.state = STOPPED
        st.last_update = approved_events(task).aggregate(m=Max("created_at"))["m"]
        st.note = f"останні дані {ago(st.last_update, now)}"
        return st

    if task.pipeline == AnalysisTask.PIPELINE_INFOSPACE:
        _infospace(task, now, st)
    elif task.pipeline == AnalysisTask.PIPELINE_TGSEARCH:
        _tgsearch(task, now, st)
    else:
        _run_based(task, now, st)

    # Черга обробки, що стоїть, — проблема незалежно від конвеєра.
    n_queue, oldest, waiting = _queue(task, now)
    if n_queue and oldest and oldest < now - timedelta(hours=_STUCK_QUEUE_HOURS):
        if st.state == LIVE:
            st.state, st.note = PROBLEM, f"обробка зупинилась ({ago(oldest, now)})"
    elif n_queue and not st.activity:
        st.activity = "обробляємо зібране"
    if waiting and not st.activity:
        st.activity = "зібране чекає обробки"
    return st
