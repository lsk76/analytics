"""
Stage worker — polls the DB for posts/chunks at its stage and processes them.

    python manage.py run_worker --stage collect
    python manage.py run_worker --stage enrich
    python manage.py run_worker --stage precluster
    python manage.py run_worker --stage classify
    python manage.py run_worker --stage dedup

    # options
    --task <slug>     only this task (default: all active tasks)
    --interval <sec>  idle sleep when there is no work (default 10)
    --once            do a single pass and exit (no loop)
"""
import time

from django.core.management.base import BaseCommand, CommandError

from analysis.models import AnalysisTask
from analysis.services import stages


class Command(BaseCommand):
    help = "Run a pipeline stage worker (claim-based, resumable)"

    def add_arguments(self, parser):
        parser.add_argument("--stage", required=True, choices=list(stages.STAGE_RUNNERS))
        parser.add_argument("--task", default=None, help="task slug (default: all active)")
        parser.add_argument("--interval", type=float, default=10.0)
        parser.add_argument("--once", action="store_true")

    def _tasks(self, slug):
        qs = AnalysisTask.objects.all()
        if slug:
            qs = qs.filter(slug=slug)
        elif hasattr(AnalysisTask, "is_active"):
            qs = qs.filter(is_active=True)
        return list(qs)

    def handle(self, *args, **opts):
        stage = opts["stage"]
        runner = stages.STAGE_RUNNERS[stage]
        if opts["task"] and not AnalysisTask.objects.filter(slug=opts["task"]).exists():
            raise CommandError(f"Задачу '{opts['task']}' не знайдено")

        self.stdout.write(self.style.SUCCESS(f"worker[{stage}] старт"))
        while True:
            did_work = False
            for task in self._tasks(opts["task"]):
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
