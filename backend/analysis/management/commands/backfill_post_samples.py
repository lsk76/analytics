"""Прив'язати вже зібрані коментарі до паспортів вибірки.

Вага коментаря (скільки реальних стоїть за одним зібраним) береться з паспорта
через Post.sample. Поле з'явилось пізніше за самі збори, тож у вже зібраного
воно порожнє, і такий коментар важить 1 — тобто графік частки поводиться як до
зважування. Ця команда проставляє зв'язок заднім числом: чат збігається, дата
коментаря лежить у періоді паспорта.

Періоди збору однієї задачі не перекриваються (перевірка стоїть у sample_collect),
тож коментар потрапляє рівно в один паспорт. Якщо перекриття все-таки є —
команда його НЕ вгадує, а показує і пропускає.
"""
from django.core.management.base import BaseCommand
from django.db.models import F

from analysis.models import MonitorSample, Post


class Command(BaseCommand):
    help = "Проставити Post.sample для вже зібраних коментарів (вага в графіку частки)."

    def add_arguments(self, parser):
        parser.add_argument("--task", default="", help="slug задачі; порожньо = всі")
        parser.add_argument("--dry-run", action="store_true",
                            help="Лише показати, скільки коментарів прив'язалось би.")

    def handle(self, *args, **o):
        samples = MonitorSample.objects.select_related("task", "channel")
        if o["task"]:
            samples = samples.filter(task__slug=o["task"])
        samples = list(samples.order_by("task_id", "channel_id", "period_start"))
        if not samples:
            self.stdout.write("паспортів вибірки немає — нічого прив'язувати")
            return

        # Перекриття періодів ламає однозначність: такий паспорт пропускаємо.
        clashing = set()
        for i, a in enumerate(samples):
            for b in samples[i + 1:]:
                if (a.task_id, a.channel_id) != (b.task_id, b.channel_id):
                    continue
                if a.period_start <= b.period_end and b.period_start <= a.period_end:
                    clashing.update({a.id, b.id})
                    self.stdout.write(self.style.WARNING(
                        f"⚠ {a.task.slug}/{a.channel}: періоди {a.period_start}…{a.period_end} "
                        f"і {b.period_start}…{b.period_end} перекриваються — пропускаю обидва"))

        total = 0
        for smp in samples:
            if smp.id in clashing:
                continue
            posts = Post.objects.filter(
                task_id=smp.task_id, channel_id=smp.channel_id, sample__isnull=True,
                posted_at__date__gte=smp.period_start, posted_at__date__lte=smp.period_end)
            n = posts.count() if o["dry_run"] else posts.update(sample=smp)
            total += n
            if n:
                self.stdout.write(f"  {smp.task.slug} {smp.channel} "
                                  f"{smp.period_start}…{smp.period_end}: {n:,} коментарів "
                                  f"(вага {smp.weight:.1f})")

        left = Post.objects.filter(sample__isnull=True,
                                   task__mon_collect_source="tg_sample").count()
        self.stdout.write(self.style.SUCCESS(
            f"{'показано' if o['dry_run'] else 'прив''язано'} {total:,} коментарів; "
            f"без паспорта лишилось {left:,} (вони важать 1)"))
