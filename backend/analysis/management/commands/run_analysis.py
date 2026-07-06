"""
Run an analysis task end-to-end, producing a ResearchRun with events + stats.

    python manage.py run_analysis <task-slug>
    python manage.py run_analysis ethnic-clashes --from 2025-01-01 --to 2025-12-31
    python manage.py run_analysis ethnic-clashes --title "Червень 2025" --from 2025-06-01 --to 2025-06-30
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from analysis.models import AnalysisTask, ResearchRun
from analysis.services.pipeline import run_pipeline


class Command(BaseCommand):
    help = "Run an AnalysisTask pipeline and store results as a ResearchRun"

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("--from", dest="date_from", required=True)
        parser.add_argument("--to", dest="date_to", required=True)
        parser.add_argument("--title", default="")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["slug"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Задачу '{opts['slug']}' не знайдено")

        dfrom = date.fromisoformat(opts["date_from"])
        dto = date.fromisoformat(opts["date_to"])

        run = ResearchRun.objects.create(
            task=task, title=opts["title"], date_from=dfrom, date_to=dto, status="pending")
        self.stdout.write(f"Запуск #{run.id}: {task.slug} {dfrom}…{dto}")

        run_pipeline(run)

        run.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(
            f"Готово. Постів: {run.posts_collected}, релевантних: {run.posts_relevant}, "
            f"подій: {run.events_total} (підтверджених: {run.events_corroborated})"))
        self.stdout.write("По місяцях: " + str(run.stats.get("by_month", {})))
