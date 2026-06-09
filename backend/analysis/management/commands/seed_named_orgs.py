"""Seed канонічних NAMED-organizations для категорії 'group'.

Категорія 'group' відкрита (LLM може додавати нові імена), але вона має
**канонічну** форму для відомих організацій — щоб варіанти написання
(«Русская община» / «Руська община» / «русская общин») збігалися в один тег.

Запуск:  python manage.py seed_named_orgs
"""
from django.core.management.base import BaseCommand

from analysis.models import Tag, TagAlias


# canonical → список лоу-кейс aliases, що згортаються у канон
NAMED_ORGS = {
    "Русская община": [
        "русская община", "руська община", "руська общіна",
        "русской общины", "русскую общину", "русским общинам",
        "руської общини", "руську общину", "русская общин",
    ],
    "Северный человек": [
        "северный человек", "северного человека", "северному человеку",
        "северные люди", "сєверний чєловєк",
    ],
    "Російський імперський рух": [
        "российский имперский", "российского имперского", "российскому имперскому",
        "російський імперський рух", "російського імперського", "р.и.м.",
    ],
    "Чорна братва": [
        "чёрная братва", "черная братва", "чорна братва", "чёрной братвы",
        "черной братвы", "чорної братви",
    ],
    "NSWP": [
        "nswp", "н.с.в.п.",
    ],
    "Хесовські": [
        "хесовские", "хесовські", "хесовских", "хесовських",
    ],
    "Имперский легион": [
        "имперский легион", "имперского легиона", "імперський легіон",
    ],
    # на майбутнє — лишаю шаблон, додавай тут інші відомі організації
    # "БАРС":             ["барс"],
    # "White Noise 88":   ["white noise 88", "белый шум 88"],
}


class Command(BaseCommand):
    help = "Seed канонічних named-organizations у категорії 'group'"

    def handle(self, *args, **opts):
        new_tags = 0
        new_aliases = 0
        for canonical, aliases in NAMED_ORGS.items():
            tag, created = Tag.objects.get_or_create(category="group", name=canonical)
            if created:
                new_tags += 1
            for raw in aliases:
                _, a_created = TagAlias.objects.get_or_create(raw=raw.lower(),
                                                              defaults={"tag": tag})
                if a_created:
                    new_aliases += 1
        self.stdout.write(self.style.SUCCESS(
            f"Named organizations: {len(NAMED_ORGS)} canonical "
            f"(нових тегів {new_tags}, нових аліасів {new_aliases})"))
