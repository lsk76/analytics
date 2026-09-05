"""
Stage worker — polls the DB for posts/chunks at its stage and processes them.

    # events pipeline
    python manage.py run_worker --stage collect
    python manage.py run_worker --stage enrich
    python manage.py run_worker --stage precluster
    python manage.py run_worker --stage classify
    python manage.py run_worker --stage dedup

    # opinion-monitor pipeline (AnalysisTask.pipeline == "monitor")
    python manage.py run_worker --stage mon_collect
    python manage.py run_worker --stage mon_filter
    python manage.py run_worker --stage mon_prescreen
    python manage.py run_worker --stage mon_tag
    python manage.py run_worker --stage mon_runs   # ранер запусків (гібрид)

    # infospace pipeline (AnalysisTask.pipeline == "infospace") — Phase 1:
    python manage.py run_worker --stage info_collect   # полінг джерел (taskless)
    python manage.py run_worker --stage info_screen
    python manage.py run_worker --stage info_event

    # accounts: тестовий прогін бота (taskless, черга accounts.TestBotJob)
    python manage.py run_worker --stage test_bot

    # options
    --task <slug>     only this task (default: all active tasks of the
                      stage's pipeline — mon_* → "monitor", res_* → "research",
                      info_* → "infospace", решта → "events"; taskless-стадії
                      TASKLESS_STAGES ігнорують --task)
    --interval <sec>  idle sleep when there is no work (default 10)
    --once            do a single pass and exit (no loop)
"""
import time

from django.core.management.base import BaseCommand, CommandError

from analysis.models import AnalysisTask
from analysis.services import (monitor_stages, pipeline_runs, research_stages,
                               stages, tgsearch_stages)

ALL_RUNNERS = {**stages.STAGE_RUNNERS, **monitor_stages.STAGE_RUNNERS,
               # гібридні ранери запусків (див. services/pipeline_runs.py):
               # mon_runs — критика (тегування агентами), ev_runs — події
               # (агент-аудит). Іменування: mon_* бачить monitor-задачі,
               # решта — events (див. _tasks нижче).
               "mon_runs": pipeline_runs.mon_runs_once,
               "ev_runs": pipeline_runs.ev_runs_once,
               # research-конвеєр (тематичні дослідження, services/research_stages.py)
               "res_collect": monitor_stages.mon_collect_once,
               "res_filter": research_stages.res_filter_once,
               "res_runs": research_stages.res_runs_once,
               # tgsearch-конвеєр (services/tgsearch_stages.py):
               # пошук у чатах через Telegram -> ШІ-фільтр -> теги -> подія
               **tgsearch_stages.STAGE_RUNNERS}

# infospace-стадії (services/infospace/stages.py). Захисний імпорт: якщо
# опційні deps (feedparser/trafilatura) не встановлені, наявні воркери
# (events/monitor/research) все одно стартують — недоступні лише info_*-стадії.
try:
    from analysis.services.infospace import stages as infospace_stages
    ALL_RUNNERS.update(infospace_stages.STAGE_RUNNERS)
except ImportError as _e:  # noqa: BLE001
    import sys
    print(f"[run_worker] infospace-стадії недоступні: {_e!r}", file=sys.stderr)

# publish-стадія (services/publish/stages.py): approved-Event → AI-фільтр+рерайт
# → Telegram Bot API. Захисний імпорт (залежить від `requests`).
try:
    from analysis.services.publish import stages as publish_stages
    ALL_RUNNERS.update(publish_stages.STAGE_RUNNERS)
except ImportError as _e:  # noqa: BLE001
    import sys
    print(f"[run_worker] publish-стадія недоступна: {_e!r}", file=sys.stderr)

# accounts: тестовий прогін бота (services/test_bot_stage.py, черга TestBotJob).
# Захисний імпорт (залежить від telethon, вже потрібного і для check_alive/authorize).
try:
    from accounts.services.test_bot_stage import test_bot_once
    ALL_RUNNERS["test_bot"] = test_bot_once
except ImportError as _e:  # noqa: BLE001
    import sys
    print(f"[run_worker] test_bot-стадія недоступна: {_e!r}", file=sys.stderr)

# Стадії, що працюють НЕ по задачах (ранер викликається без аргументу).
# info_collect полить ДЖЕРЕЛА (Source): одне джерело живить кілька задач,
# тож цикл «for task» для нього не має сенсу — черга полінгу глобальна.
# info_healthcheck — так само по джерелах (dry-run якості web/rss).
# Ретеншн (info_retention) — навпаки, ПЕР-ЗАДАЧНА операція (чистить пости
# task.info_retention_days), тож іде звичайним циклом задач (не тут).
# publish — теж по НЕ-задачах: ітерує PublishConfig-профілі, а не AnalysisTask.
TASKLESS_STAGES = {"info_collect", "info_healthcheck", "publish", "test_bot"}


class Command(BaseCommand):
    help = "Run a pipeline stage worker (claim-based, resumable)"

    def add_arguments(self, parser):
        parser.add_argument("--stage", required=True, choices=list(ALL_RUNNERS))
        parser.add_argument("--task", default=None, help="task slug (default: all active)")
        parser.add_argument("--interval", type=float, default=10.0)
        parser.add_argument("--once", action="store_true")

    def _tasks(self, slug, stage):
        qs = AnalysisTask.objects.all()
        if slug:
            return list(qs.filter(slug=slug))
        qs = qs.filter(is_active=True)
        # кожен воркер бачить лише задачі «свого» конвеєра: events-воркер не
        # повинен claim'ити чанки monitor-задачі (і навпаки)
        if stage == "review":
            # авто-аудит подій спільний для events і infospace (обидва мають
            # review_enabled + Event.review_status); review_once сам no-op, якщо
            # review_enabled=False (дефолт infospace), тож це бекс-сумісно
            return list(qs.filter(pipeline__in=[AnalysisTask.PIPELINE_EVENTS,
                                                AnalysisTask.PIPELINE_INFOSPACE]))
        if stage.startswith("mon_"):
            pipeline = AnalysisTask.PIPELINE_MONITOR
        elif stage.startswith("res_"):
            pipeline = AnalysisTask.PIPELINE_RESEARCH
        elif stage.startswith("info_"):
            pipeline = AnalysisTask.PIPELINE_INFOSPACE
        elif stage.startswith("tgs_"):
            pipeline = AnalysisTask.PIPELINE_TGSEARCH
        else:
            pipeline = AnalysisTask.PIPELINE_EVENTS
        return list(qs.filter(pipeline=pipeline))

    def handle(self, *args, **opts):
        stage = opts["stage"]
        runner = ALL_RUNNERS[stage]
        if opts["task"] and not AnalysisTask.objects.filter(slug=opts["task"]).exists():
            raise CommandError(f"Задачу '{opts['task']}' не знайдено")

        self.stdout.write(self.style.SUCCESS(f"worker[{stage}] старт"))
        while True:
            did_work = False
            if stage in TASKLESS_STAGES:
                # стадія по джерелах (без циклу задач): ранер сам claim'ить
                # одиницю роботи з глобальної черги і повертає True, поки є що робити
                try:
                    while runner():
                        did_work = True
                        if opts["once"]:
                            break
                except Exception as e:  # noqa: BLE001 — keep the worker alive
                    self.stderr.write(f"worker[{stage}]: {e!r}")
            else:
                for task in self._tasks(opts["task"], stage):
                    try:
                        # drain this task's queue for the stage until empty
                        while runner(task):
                            did_work = True
                            if opts["once"]:
                                break
                    except Exception as e:  # noqa: BLE001 — keep the worker alive
                        self.stderr.write(f"worker[{stage}] {task.slug}: {e!r}")
            if opts["once"]:
                break
            if not did_work:
                time.sleep(opts["interval"])
