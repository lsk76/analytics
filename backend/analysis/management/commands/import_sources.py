"""
Сідер джерел infospace з CSV/JSON + (опційно) підписка задачі.

CSV-колонки (заголовок обов'язковий): kind,name,url,region,language,scraper_key
  kind      — telegram|rss|web
  region    — канонічна назва Region (напр. «Бурятія»); порожньо = без гео
  scraper_key, language — опційні

    python manage.py import_sources sources.csv
    python manage.py import_sources sources.csv --subscribe <task-slug>
    python manage.py import_sources sources.json --format json

JSON: список об'єктів із тими самими ключами.
Ідемпотентно: джерело апсертиться по (kind, url); підписка — по (task, source).
"""
import csv
import json

from django.core.management.base import BaseCommand, CommandError

from analysis.models import AnalysisTask, Region, Source, SourceSubscription
from analysis.services.infospace.utils import canonical_url


class Command(BaseCommand):
    help = "Імпорт джерел infospace з CSV/JSON"

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--format", choices=["csv", "json"], default="csv")
        parser.add_argument("--subscribe", default=None,
                            help="slug задачі, на яку підписати всі імпортовані джерела")

    def _rows(self, path, fmt):
        with open(path, encoding="utf-8") as f:
            if fmt == "json":
                return json.load(f)
            return list(csv.DictReader(f))

    def handle(self, *args, **opts):
        task = None
        if opts["subscribe"]:
            task = AnalysisTask.objects.filter(slug=opts["subscribe"]).first()
            if not task:
                raise CommandError(f"Задачу '{opts['subscribe']}' не знайдено")
            if task.pipeline != AnalysisTask.PIPELINE_INFOSPACE:
                self.stderr.write(self.style.WARNING(
                    f"Увага: задача {task.slug} має pipeline={task.pipeline}, не infospace"))

        rows = self._rows(opts["path"], opts["format"])
        n_src_new = n_src_upd = n_sub = 0
        region_cache = {}
        for r in rows:
            kind = (r.get("kind") or "").strip()
            url = (r.get("url") or "").strip()
            name = (r.get("name") or "").strip() or url
            if not kind or not url:
                self.stderr.write(f"пропуск рядка без kind/url: {r}")
                continue
            # rss/web канонізуємо; telegram-хендли лишаємо як є (canonical_url їх не чіпає)
            url = canonical_url(url) if kind in ("rss", "web") else url

            region = None
            rname = (r.get("region") or "").strip()
            if rname:
                if rname not in region_cache:
                    region_cache[rname] = Region.objects.filter(name=rname).first()
                region = region_cache[rname]
                if region is None:
                    self.stderr.write(self.style.WARNING(
                        f"регіон '{rname}' не знайдено — джерело {name} без гео"))

            src, created = Source.objects.update_or_create(
                kind=kind, url=url,
                defaults=dict(name=name, region_subject=region,
                              language=(r.get("language") or "").strip(),
                              scraper_key=(r.get("scraper_key") or "").strip()),
            )
            n_src_new += int(created)
            n_src_upd += int(not created)
            if task:
                _, sub_created = SourceSubscription.objects.get_or_create(
                    task=task, source=src)
                n_sub += int(sub_created)

        self.stdout.write(self.style.SUCCESS(
            f"Джерела: +{n_src_new} нових, {n_src_upd} оновлено. "
            f"Підписок: +{n_sub}" + (f" на {task.slug}" if task else "")))
