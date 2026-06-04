"""
Seed the "ethnic-clashes" AnalysisTask (validated query + classification prompt).

    python manage.py seed_ethnic_clashes
"""
from datetime import date

from django.core.management.base import BaseCommand

from analysis.models import AnalysisTask

ETHNIC = (
    "мигрант приезж диаспора этническ межнациональн межэтническ нерусск гастарбайтер нелегал "
    'кавказец кавказц "лицо кавказской" "выходец из" "уроженец" '
    "чеченец чеченц ингуш дагестан аварец даргинец лезгин кумык лакец табасаран "
    "осетин кабардинец балкарец карачаевец черкес адыгеец ногаец "
    "татарин татар башкир чуваш мордвин удмурт мариец кряшен "
    "бурят якут саха тувинец тувинц хакас алтаец калмык ненец эвенк "
    "таджик узбек киргиз кыргыз казах туркмен "
    "азербайджанец армянин армян грузин цыган"
)
CONFLICT = (
    "драка избил напал поножовщина зарезал пырнул избиение потасовка погром стычка "
    'расправа оскорбил унижал угрожал изнасилов дебош "с ножом" "на национальной почве"'
)
EXCL = (
    "-(всу зсу фронт окоп артиллер дрон шахед ракет обстрел авиаудар пво военкомат "
    "мобилизаци полигон воин неприятел псалом псалтирь богослуж) "
    "-(##спам ##перевозки ##продажа ##робота ##нерухомість)"
)
QUERY = f"({ETHNIC}) +({CONFLICT}) {EXCL}"

# DOMAIN rules only — the framework auto-appends the JSON schema from the task's
# tag categories (+ geo fields). Do NOT put the JSON schema here.
CLASSIFY_PROMPT = (
    "Ти — аналітик OSINT. Класифікуєш повідомлення з Telegram щодо МІЖЕТНІЧНИХ "
    "насильницьких інцидентів у Росії.\n"
    "Міжетнічний інцидент = РЕАЛЬНЕ насильство/конфлікт з ЯВНИМ етнічним виміром між людьми "
    "РІЗНИХ етносів (мігрант напав на місцевого, бійка на національному ґрунті, погром, "
    "конфлікт діаспори й місцевих тощо). Має бути щонайменше ОДНА конкретна етнічна/"
    "національна/міграційна сторона.\n"
    "is_relevant=false, якщо:\n"
    "- немає етнічного виміру (побутова сварка, борг, ДТП, конфлікт у межах одного етносу);\n"
    "- етнічність учасників невідома/не вказана (просто «невідомі», «хлопці», «група осіб»);\n"
    "- це лише онлайн-сварка/коментарі/образи в мережі без реального інциденту;\n"
    "- йдеться про тварин/майно, а не про насильство між людьми;\n"
    "- війна, спорт, ігри, суто політичний текст без конкретного інциденту; подія ПОЗА Росією.\n"
    "У теги-сторони став лише РЕАЛЬНІ групи учасників; якщо сторона невідома — пропусти її "
    "(краще одна конкретна сторона, ніж заглушка).\n"
    "Значення тегів пиши українською, узагальнено (напр. таджик, русский, чеченець, мігрант)."
)


class Command(BaseCommand):
    help = "Seed the ethnic-clashes AnalysisTask"

    def handle(self, *args, **opts):
        task, created = AnalysisTask.objects.update_or_create(
            slug="ethnic-clashes",
            defaults={
                "name": "Етнічні сутички в РФ",
                "description": "Міжетнічні насильницькі інциденти в Росії за даними Telegram.",
                "telezip_query": QUERY,
                "date_from": date(2025, 1, 1),
                "date_to": date(2025, 12, 31),
                "languages": ["ru"],
                "search_posts": True,
                "search_comments": False,
                "collect_chunk_days": 1,
                "telezip_unique": False,   # збирати всі репости -> точне охоплення
                "classify_system_prompt": CLASSIFY_PROMPT,
                "relevance_field": "is_relevant",
                "dedup_window_days": 2,
                "dedup_pre_thresh": 82,
                "dedup_cand_thresh": 55,
                "llm_model": "google/gemini-2.5-flash",
                # domain config: nationalities are a closed seed list; geo on;
                # dedup judge prompt + generic sides left blank => ethnic defaults
                "geo_enabled": True,
                "closed_tag_categories": ["nationality"],
            },
        )
        # tag categories this task collects (schema built from these)
        from analysis.models import TagCategory
        cats = TagCategory.objects.filter(
            key__in=["nationality", "status", "religion", "role", "group", "conflict"])
        task.tag_categories.set(cats)
        self.stdout.write(self.style.SUCCESS(
            f"{'Створено' if created else 'Оновлено'} задачу '{task.slug}' (#{task.id}); "
            f"категорій: {task.tag_categories.count()}"))
