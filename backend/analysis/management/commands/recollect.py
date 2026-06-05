"""
Re-run a period for a task.

    # re-run the pipeline on already-collected posts (no TeleZip)
    python manage.py recollect ethnic-clashes --from 2026-05-01 --to 2026-05-03 --reprocess

    # wipe the period and collect again from TeleZip
    python manage.py recollect ethnic-clashes --from 2026-05-01 --to 2026-05-03 --fresh
"""
import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from analysis.models import AnalysisTask, ResearchRun
from analysis.services import stages
from django.utils import timezone as djtz


def _date(s):
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


class Command(BaseCommand):
    help = "Re-run a period: --reprocess (no TeleZip) or --fresh (re-collect)"

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("--from", dest="dfrom", required=True, type=_date)
        parser.add_argument("--to", dest="dto", required=True, type=_date)
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--reprocess", action="store_true",
                       help="re-run pipeline on existing posts (no TeleZip)")
        g.add_argument("--fresh", action="store_true",
                       help="wipe period and collect again from TeleZip")

    def handle(self, *args, **o):
        task = AnalysisTask.objects.filter(slug=o["slug"]).first()
        if not task:
            raise CommandError(f"Задачу '{o['slug']}' не знайдено")
        a, b = o["dfrom"], o["dto"]
        if a > b:
            raise CommandError("--from пізніше за --to")

        if o["reprocess"]:
            n_ev, n_posts = stages.reprocess_period(task, a, b)
            self.stdout.write(self.style.SUCCESS(
                f"reprocess {a}…{b}: -{n_ev} подій, скинуто {n_posts} постів → collected. "
                f"Воркери доведуть назад до подій."))
        else:
            job = ResearchRun.objects.create(
                task=task, title=f"recollect {djtz.now():%Y-%m-%d %H:%M}",
                date_from=a, date_to=b, chunk_days=task.collect_chunk_days or 1,
                status="collecting")
            n_ev, n_posts, n_chunks = stages.recollect_fresh(task, a, b, job=job)
            self.stdout.write(self.style.SUCCESS(
                f"fresh {a}…{b}: -{n_ev} подій, -{n_posts} постів, job #{job.id}, "
                f"+{n_chunks} чанків у черзі."))
