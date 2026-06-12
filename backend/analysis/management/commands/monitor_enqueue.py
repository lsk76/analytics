"""
Enqueue collection period for an opinion-monitor task.

Створює pending CollectChunk-и (1-денні за замовчуванням — TeleZip 500-ить на
широких вікнах з cluster-query); далі worker-mon-collect сам їх розгрібає, а
filter/prescreen/tag-воркери доводять пости до done. Ніяких ручних кроків.

  python manage.py monitor_enqueue --task dagestan-criticism-monitor \\
      --from 2026-01-04 --to 2026-01-31
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as djtz

from analysis.models import AnalysisTask, ResearchRun
from analysis.services import stages


class Command(BaseCommand):
    help = "Enqueue CollectChunks for a monitor task (workers do the rest)."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True)
        parser.add_argument("--from", dest="date_from", required=True,
                            help="YYYY-MM-DD (UTC).")
        parser.add_argument("--to", dest="date_to", required=True,
                            help="YYYY-MM-DD (UTC).")
        parser.add_argument("--chunk-days", type=int, default=1,
                            help="Розмір чанка (default 1 — ширші вікна TeleZip "
                                 "не тягне з cluster-exclusion query).")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Task {opts['task']!r} not found.")
        if task.pipeline != AnalysisTask.PIPELINE_MONITOR:
            raise CommandError(
                f"Task {task.slug} has pipeline={task.pipeline!r}; set it to "
                f"'monitor' first (admin or shell) so mon_* workers pick it up.")

        df = date.fromisoformat(opts["date_from"])
        dt = date.fromisoformat(opts["date_to"])
        # ResearchRun = «job»: дає картку у /admin/analysis/researchrun/status/
        # з прогресом чанків і стадій — без нього збір невидимий в адмінці.
        run = ResearchRun.objects.create(
            task=task, title=f"monitor {df}..{dt}",
            date_from=df, date_to=dt,
            chunk_days=opts["chunk_days"], status="collecting",
            started_at=djtz.now(),
        )
        made = stages.enqueue_collection(task, df, dt,
                                         chunk_days=opts["chunk_days"], job=run)
        self.stdout.write(self.style.SUCCESS(
            f"job #{run.id}: enqueued {made} chunks for {task.slug} {df}..{dt} "
            f"(chunk={opts['chunk_days']}d). worker-mon-collect забере сам; "
            f"прогрес: /admin/analysis/researchrun/status/"
        ))
