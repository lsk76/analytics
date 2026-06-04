"""
Seed the CLOSED conflict-type vocabulary as Tags (category='conflict') + aliases.

    python manage.py seed_conflict_types

Conflict types live in the unified Tag system (category='conflict'); resolve_conflict_tag
picks exactly one of these — no new/duplicate types are ever created.
"""
from django.core.management.base import BaseCommand

from analysis.models import Tag, TagAlias

# canonical (uk) -> raw variants that map to it
CONFLICT_TYPES = {
    "напад": ["нападение", "напад", "разбой", "разбойное нападение", "нападение с ножом"],
    "бійка": ["бійка", "драка", "избиение", "избиение", "потасовка", "побои", "поножовщина"],
    "вбивство": ["вбивство", "убийство", "зарезал", "убил"],
    "погроза": ["погроза", "угроза", "угрозы"],
    "образа": ["образа", "оскорбление", "унижение"],
    "зґвалтування": ["зґвалтування", "изнасилование", "насилие сексуальное"],
    "погром": ["погром", "погромы"],
    "масові заворушення": ["масові заворушення", "массовые беспорядки", "массовая драка", "бунт"],
    "викрадення": ["викрадення", "похищение", "похищення", "похищение человека"],
    "вимагання": ["вимагання", "вымогательство", "рэкет"],
    "підпал": ["підпал", "поджог"],
    "інше": ["інше", "иное", "прочее", "другое"],
}


class Command(BaseCommand):
    help = "Seed closed conflict-type Tags (category='conflict') + aliases"

    def handle(self, *args, **opts):
        created_t = created_a = 0
        for canon, variants in CONFLICT_TYPES.items():
            tag, c = Tag.objects.get_or_create(name=canon, category="conflict")
            created_t += int(c)
            for raw in {canon, *variants}:
                key = raw.strip().lower()
                if not key:
                    continue
                _, ca = TagAlias.objects.get_or_create(raw=key, defaults={"tag": tag})
                created_a += int(ca)
        total = Tag.objects.filter(category="conflict").count()
        self.stdout.write(self.style.SUCCESS(
            f"Типи конфлікту: всього {total} (нових {created_t}), нових аліасів {created_a}"))
