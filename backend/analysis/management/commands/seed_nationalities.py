"""Seed the closed canonical list of nationalities as Tags (category=nationality).

    python manage.py seed_nationalities
"""
from django.core.management.base import BaseCommand

from analysis.models import Tag

NATIONALITIES = [
    # East Slavs
    "росіянин", "українець", "білорус",
    # North Caucasus
    "чеченець", "інгуш", "дагестанець", "аварець", "даргинець", "лезгин", "кумик",
    "лакець", "табасаран", "осетин", "кабардинець", "балкарець", "карачаєвець",
    "черкес", "адигеєць", "ногаєць",
    # Volga / Urals
    "татарин", "башкир", "чуваш", "мордвин", "удмурт", "марієць", "комі", "кряшен",
    # Siberia / North
    "бурят", "якут", "тувинець", "хакас", "алтаєць", "калмик", "ненець", "евенк",
    # Central Asia / Caucasus diaspora
    "таджик", "узбек", "киргиз", "казах", "туркмен",
    "азербайджанець", "вірменин", "грузин",
    # Generalized / other
    "кавказець", "циган", "єврей", "араб", "китаєць", "кореєць",
]


class Command(BaseCommand):
    help = "Seed canonical nationalities (closed list, category=nationality)"

    def handle(self, *args, **opts):
        n = 0
        for name in NATIONALITIES:
            _, created = Tag.objects.get_or_create(name=name, category="nationality")
            n += int(created)
        total = Tag.objects.filter(category="nationality").count()
        self.stdout.write(self.style.SUCCESS(f"Націй у БД: {total} (нових: {n})"))
