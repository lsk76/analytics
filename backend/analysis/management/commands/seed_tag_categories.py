"""Seed the global TagCategory registry.  python manage.py seed_tag_categories"""
from django.core.management.base import BaseCommand

from analysis.models import TagCategory

CATEGORIES = [
    ("nationality", "Національність", True, 10),
    ("status", "Статус (мігрант/місцеві)", False, 20),
    ("religion", "Релігія", False, 30),
    ("role", "Роль/професія/вік", False, 40),
    ("group", "Організація/спільнота", False, 50),
    ("conflict", "Тип конфлікту", True, 60),
    ("other", "Інше", False, 100),
]


class Command(BaseCommand):
    help = "Seed TagCategory registry"

    def handle(self, *args, **opts):
        n = 0
        for key, label, closed, order in CATEGORIES:
            _, created = TagCategory.objects.update_or_create(
                key=key, defaults={"label": label, "closed": closed, "order": order})
            n += int(created)
        self.stdout.write(self.style.SUCCESS(
            f"Категорії тегів: всього {TagCategory.objects.count()} (нових {n})"))
