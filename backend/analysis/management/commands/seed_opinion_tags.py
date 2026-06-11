"""
Seed taxonomy for the opinion-monitor pipeline.

Idempotent: re-running just upserts.

Creates:
  * 3 TagCategory rows: criticism_target, topic, opinion
  * All initial Tag rows for each category
  * (optional) AnalysisTask for one region (--task-slug, --region)

Example:
  python manage.py seed_opinion_tags
  python manage.py seed_opinion_tags --task-slug dagestan-criticism-monitor \
      --task-name "Дагестан: моніторинг критики" \
      --region-label "Республіка Дагестан"
"""
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from analysis.models import AnalysisTask, Tag, TagCategory


CATEGORIES = [
    {
        "key": "criticism_target",
        "label": "Об'єкт критики",
        "closed": False,
        "hint": "Кого/що критикує коментар. Можна створювати нові теги для конкретних осіб.",
        "order": 50,
    },
    {
        "key": "topic",
        "label": "Тема",
        "closed": True,
        "hint": "Про яку політичну тему говорить коментар.",
        "order": 60,
    },
    {
        "key": "opinion",
        "label": "Тип думки",
        "closed": True,
        "hint": "Стиль / тип висловлювання (критика, підтримка, сарказм, пропаганда…).",
        "order": 70,
    },
]


# tag name -> category
INITIAL_TAGS = {
    "criticism_target": [
        # M1 — fed gov
        "крит_путін", "крит_кремль", "крит_думи", "крит_уряду", "крит_совбезу",
        "крит_МО", "крит_ФСБ", "крит_МВД", "крит_росгвардії",
        "крит_прокуратури", "крит_єдиної_росії",
        # M2 umbrella
        "крит_рішень_центру_щодо_регіону",
        # Regional
        "крит_глави_регіону", "крит_мера", "крит_рег_правит",
        "крит_місц_депутата", "крит_губернатор_іншого_регіону",
        # Religious authorities (Дагестан — муфтіят, окремі шейхи)
        "крит_релігійних_авторитетів",
    ],
    "topic": [
        "тема_СВО", "тема_мобілізації", "тема_економіки", "тема_корупції",
        "тема_репресій", "тема_інфраструктури", "тема_релігії", "тема_етнічна",
    ],
    "opinion": [
        "підтримка_влади", "нейтрально_новина", "сарказм", "пропаганда",
        "теорія_змови", "прогноз_влади",
    ],
}


DEFAULT_TAGGER_PROMPT = (
    "(заповнюється LLM-агентом у monitor_prepare_batches; див. analysis/pilot/prompts.py)"
)


class Command(BaseCommand):
    help = "Seed opinion-monitor tag taxonomy (3 categories + initial tags), "\
           "optionally also a starter AnalysisTask for a region."

    def add_arguments(self, parser):
        parser.add_argument("--task-slug", default="",
                            help="Slug нової AnalysisTask. Якщо порожньо — не створюється.")
        parser.add_argument("--task-name", default="",
                            help="Назва AnalysisTask (українською).")
        parser.add_argument("--region-label", default="",
                            help="Регіон для опису задачі.")
        parser.add_argument("--date-from", default="2025-01-01")
        parser.add_argument("--date-to", default="2030-12-31",
                            help="Широке вікно — обмежимо при кожному collect-ранi.")

    @transaction.atomic
    def handle(self, *args, **opts):
        # 1. categories
        cats = {}
        for c in CATEGORIES:
            obj, created = TagCategory.objects.update_or_create(
                key=c["key"],
                defaults={k: v for k, v in c.items() if k != "key"},
            )
            cats[c["key"]] = obj
            self.stdout.write(
                f"  category {c['key']:18s} {'+ new' if created else '· upd'}"
            )

        # 2. tags
        total_new = 0
        for cat_key, names in INITIAL_TAGS.items():
            for name in names:
                _, created = Tag.objects.update_or_create(
                    name=name, category=cat_key,
                    defaults={},
                )
                if created:
                    total_new += 1
        self.stdout.write(
            f"  tags total: {Tag.objects.filter(category__in=INITIAL_TAGS).count()} "
            f"(+{total_new} new)"
        )

        # 3. optional AnalysisTask
        slug = (opts["task_slug"] or "").strip()
        if slug:
            name = opts["task_name"] or f"Моніторинг: {slug}"
            region_label = opts["region_label"] or "—"
            task, created = AnalysisTask.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "description": (
                        f"Моніторинг опінії у чатах регіону «{region_label}». "
                        f"Pipeline: pilot.filters → LLM tagger. "
                        f"Whitelist чатів керується через MonitorChat у адмінці."
                    ),
                    # Не потрібні для opinion-моніторингу — порожній/0/false
                    "telezip_query": "",  # whitelist + reply filter заміняє query
                    "date_from": date.fromisoformat(opts["date_from"]),
                    "date_to": date.fromisoformat(opts["date_to"]),
                    "languages": ["ru"],
                    "search_posts": False,
                    "search_comments": True,
                    "telezip_unique": False,
                    "collect_chunk_days": 7,  # тиждень за один TeleZip-виклик
                    "classify_system_prompt": DEFAULT_TAGGER_PROMPT,
                    "relevance_field": "is_relevant",
                    "dedup_window_days": 0,   # коментарі не дедупимо як події
                    "geo_enabled": False,     # регіон = чат, не коментар
                    "review_enabled": False,
                },
            )
            # link 3 categories
            task.tag_categories.set([cats["criticism_target"],
                                     cats["topic"], cats["opinion"]])
            task.save()
            self.stdout.write(self.style.SUCCESS(
                f"  task {slug} {'created' if created else 'updated'}"
            ))

        self.stdout.write(self.style.SUCCESS("seed done"))
