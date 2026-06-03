"""
Merge semantic-duplicate vocabulary that already accumulated in the DB.

LLM clusters existing canonical names into groups (one canonical each), then
remaps aliases + Event.sides / Event.conflict_type and deletes the duplicates.

    python manage.py recanonicalize
    python manage.py recanonicalize --dry-run
"""
import json

from django.core.management.base import BaseCommand
from django.db import transaction

from analysis.models import (
    Nationality, NationalityAlias, ConflictType, ConflictTypeAlias, Event,
)
from analysis.services.normalize import _sync_llm


def _cluster(names, rule):
    prompt = (
        "Згрупуй назви, що означають ТЕ САМЕ (синоніми, різні мови, рід, відмінки).\n"
        f"Канонічна назва кожної групи — {rule}.\n"
        "Поверни СТРОГО JSON-обʼєкт {канонічна: [усі варіанти з цієї групи]}. "
        "Кожна вхідна назва має бути рівно в одній групі.\n"
        f"Назви: {json.dumps(names, ensure_ascii=False)}\n"
        "Лише JSON, без markdown."
    )
    raw = _sync_llm(prompt).strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    a, b = raw.find("{"), raw.rfind("}")
    return json.loads(raw[a:b + 1]) if a != -1 else {}


class Command(BaseCommand):
    help = "LLM-merge semantic-duplicate Nationality / ConflictType entries"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def _process(self, model, alias_model, fk, m2m, rule, dry):
        names = list(model.objects.values_list("name", flat=True))
        if not names:
            return
        groups = _cluster(names, rule)
        name_to_obj = {n.lower(): o for n, o in
                       ((o.name, o) for o in model.objects.all())}

        merged = 0
        for canonical, variants in groups.items():
            canonical = canonical.strip()
            if not canonical:
                continue
            self.stdout.write(f"  {canonical}  <-  {variants}")
            if dry:
                continue
            with transaction.atomic():
                canon_obj = (model.objects.filter(name__iexact=canonical).first()
                             or model.objects.create(name=canonical))
                for v in variants:
                    vo = name_to_obj.get(v.strip().lower())
                    # always register the variant as an alias of the canonical
                    alias_model.objects.update_or_create(
                        raw=v.strip().lower(), defaults={fk: canon_obj})
                    if not vo or vo.id == canon_obj.id:
                        continue
                    # repoint aliases of the old object
                    alias_model.objects.filter(**{fk: vo}).update(**{fk: canon_obj})
                    # repoint events
                    if m2m:
                        for ev in Event.objects.filter(sides=vo):
                            ev.sides.remove(vo)
                            ev.sides.add(canon_obj)
                    else:
                        Event.objects.filter(conflict_type=vo).update(conflict_type=canon_obj)
                    vo.delete()
                    merged += 1
        self.stdout.write(self.style.SUCCESS(
            f"{model.__name__}: злито {merged}, лишилось {model.objects.count()}"))

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        self.stdout.write("== Національності ==")
        self._process(Nationality, NationalityAlias, "nationality", True,
                       'українською, однина, чол. рід (таджик, росіянин)', dry)
        self.stdout.write("== Типи конфліктів ==")
        self._process(ConflictType, ConflictTypeAlias, "conflict_type", False,
                      'українською, узагальнено (напад, бійка, вбивство)', dry)
