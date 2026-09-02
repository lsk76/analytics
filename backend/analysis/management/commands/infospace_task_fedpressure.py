"""
Створити/оновити дослідницьку infospace-задачу «федеральний тиск» і підписати її
на TELEGRAM-джерела вже існуючої задачі-моніторингу.

Три критерії відбору (ТЗ замовника):
  K1 — протестні інциденти через невдоволення діями федеральної влади;
  K2 — згадки про дії федвлади щодо визначених суб'єктів у регіональних каналах;
  K3 — внутрішня і зовнішня міграція через політику федвлади (примусова
       мобілізація, погіршення рівня життя).

ЧОМУ ОКРЕМА ЗАДАЧА, А НЕ ПЕРЕСКРІН ІСНУЮЧОЇ: `rescreen_task_now` видаляє події
задачі, а в 13-й їх ~2180 і вона жива. Пости в infospace створюються пер-задачно
(unique(task, url)), тож дві задачі читають один потік джерел і фільтрують його
кожна своїм промптом, не заважаючи одна одній.

ЧОМУ ЛИШЕ TELEGRAM: див. докстрінг `infospace_backfill` — тільки TG віддає
історію за минулий період, і змішування джерел різної глибини зробило б
порівняння періодів артефактом покриття.

    python manage.py infospace_task_fedpressure --from-task info-buryatiya
    python manage.py infospace_task_fedpressure --from-task info-buryatiya --dry-run

ІДЕМПОТЕНТНО: задача апсертиться по slug, ПРОМПТИ ПЕРЕЗАПИСУЮТЬСЯ щоразу (щоб
тюнінг фільтра був однією правкою в git + перезапуском), підписки — по (task,
source). Щоб не затирати ручні правки промптів з адмінки — `--keep-prompts`.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from analysis.models import (AnalysisTask, Source, SourceSubscription, Tag,
                             TagCategory)

SLUG = "fed-pressure"
NAME = "Федеральний тиск: протест, дії центру, міграція"

# --- категорії тегів (ІНВАРІАНТ: нова категорія ⇒ рядок TagCategory, інакше
# --- фасет-фільтр адмінки не реєструється і URL з tag_<cat> падає на ?e=1) ---
TAG_CATS = [
    ("fed_criterion", "Критерій дослідження", 10,
     ["K1_протест", "K2_дії_федвлади", "K3_міграція"]),
    ("fed_tone", "Тон подачі", 20,
     ["тон_критичний", "тон_нейтральний", "тон_схвальний"]),
    ("fed_importance", "Важливість", 30,
     ["важливість_1", "важливість_2", "важливість_3", "важливість_4", "важливість_5"]),
]

SCREEN_PROMPT = """You are a STRICT screening FILTER for a RESEARCH monitor of FEDERAL-CENTRE PRESSURE on the regions of SIBERIA and the RUSSIAN FAR EAST. The sources are REGIONAL Telegram channels.

MONITORED REGIONS:
- SIBERIA: Altai Republic, Altai Krai, Tyva, Khakassia, Krasnoyarsk Krai, Irkutsk Oblast, Kemerovo Oblast (Kuzbass), Novosibirsk Oblast, Omsk Oblast, Tomsk Oblast;
- FAR EAST: Buryatia, Sakha (Yakutia), Zabaykalsky Krai, Kamchatka Krai, Primorsky Krai, Khabarovsk Krai, Amur Oblast, Magadan Oblast, Sakhalin Oblast, Jewish Autonomous Oblast, Chukotka Autonomous Okrug;
- NORTHERN OKRUGS of Western Siberia (indigenous peoples — Khanty, Mansi, Nenets, Selkup): Khanty-Mansi Autonomous Okrug — Yugra, Yamalo-Nenets Autonomous Okrug.
NOTHING ELSE is monitored: the North Caucasus, Tatarstan, Bashkortostan, the rest of the Urals and European Russia are OUT OF SCOPE — such an item is relevant=false with region null, even when acute. In particular TYUMEN OBLAST itself (Tyumen, Tobolsk, Ishim) and Kurgan Oblast are NOT monitored: only the two autonomous okrugs above are.

WHAT THIS MONITOR COLLECTS. Unlike a general news filter, this one keeps an item ONLY if it matches AT LEAST ONE of the three research criteria K1 / K2 / K3 below. An item can be dramatic, tragic or highly newsworthy and still be IRRELEVANT here: a flood, a fire, a road accident, an ordinary crime, a local utility failure, a regional official's scandal with no federal dimension → relevant=false. What makes an item relevant is the FEDERAL dimension (K1, K2) or POPULATION OUTFLOW (K3) — nothing else.

=== K1 — PROTEST DRIVEN BY DISCONTENT WITH THE FEDERAL AUTHORITIES ===
A collective or public act of protest by residents of a monitored region whose grievance is aimed at the FEDERAL centre or its policy.
FORMS THAT COUNT: rally, picket — INCLUDING A SINGLE-PERSON PICKET (одиночний пікет), the standard legal protest form in Russia — demonstration, march, strike or work stoppage, collective letter / appeal / petition / video address, public refusal to comply, blocking of a road or a site, a mass complaint campaign, a public confrontation with federal officials or security forces, a spontaneous crowd gathering over a grievance.
THE GRIEVANCE MUST BE AIMED AT THE FEDERAL LEVEL: mobilization, conscription or contract recruitment; the war and its costs; a federal law, decree or ministry decision; tariffs, taxes, pension or benefit rules set federally; extraction, infrastructure or waste projects licensed or imposed by the centre; language and education policy; the seizure of regional powers, revenues or property; the actions of federal security services.
ALSO COUNTS: the authorities' response to such a protest — detentions, fines, criminal or administrative cases, refusal to authorise the action, pressure on the organisers.
DOES NOT COUNT: a protest whose target is purely LOCAL or MUNICIPAL (a broken pipe, a city landfill, a local mayor) with no federal addressee; one person's angry post or comment on social media; a private complaint or lawsuit by a single person; a state-organised patriotic rally.

=== K2 — FEDERAL AUTHORITIES ACTING ON A MONITORED REGION, AS COVERED BY REGIONAL CHANNELS ===
An ACTION BY A FEDERAL ACTOR that is DIRECTED AT a monitored region or has direct consequences for it.
FEDERAL ACTORS: the President and his Administration; the plenipotentiary envoy (полпред) of the federal district; the Government and federal ministries (including the Ministry for the Development of the Far East); the State Duma and the Federation Council; federal agencies and services (Prosecutor General's Office, Investigative Committee, FSB, MVD, Rosgvardia, FNS, Rosprirodnadzor, Rostransnadzor, Rosreestr and the like); federal courts; the Central Bank; state corporations and federally-owned companies (RZhD, Gazprom, Rosneft, Rusal, RusHydro and the like).
ACTIONS THAT COUNT: decisions, decrees and laws applied to the region; the appointment, dismissal, reprimand or public assessment of a regional head or regional officials; inspections, audits, criminal cases and detentions initiated by federal bodies; budget transfers, subsidies, debt restructuring or a refusal of funding; the redistribution of powers, revenues or property between the centre and the region; federal programmes and investment projects, their imposition or cancellation; the licensing of resource extraction; restrictions, bans, mobilization quotas; a federal visit WITH A DECISION OR AN ASSESSMENT attached to it.
NO ACUTENESS FILTER APPLIES TO K2 — THIS IS DELIBERATE. Keep the item whether the federal action is presented as GOOD (a subsidy, a new programme, a promised bridge), NEUTRAL, or BAD. The research question is how much of the federal agenda is present in the regional information space and HOW it is framed; dropping the positive coverage would measure only conflict and destroy the metric. Record the framing in the tone tag instead.
DOES NOT COUNT: an all-Russia law, statistic or event that merely MENTIONS the region without a decision addressed to it; a federal politician's general speech on an all-Russia topic; a purely ceremonial greeting or anniversary message; an action by REGIONAL or municipal authorities with no federal actor involved; news about another region.

=== K3 — MIGRATION AND POPULATION OUTFLOW CAUSED BY FEDERAL POLICY ===
The departure of residents from a monitored region — internal (to other Russian regions) or external (abroad) — together with its causes, scale and consequences.
COUNTS: reports of outflow, relocation or emigration; flight from mobilization or conscription; a refusal to return; depopulation or demographic decline attributed to departures; the closure of enterprises or settlements pushing people out; young people leaving; the loss of specialists (doctors, teachers, engineers); the emptying of villages and single-industry towns.
THE CAUSE MUST BE TIED TO FEDERAL POLICY OR ITS EFFECTS: mobilization and recruitment for the war, falling living standards, unemployment, prices, the destruction of the local economy, ecological damage from centrally-licensed projects, repression.
STATISTICS AND EXPERT COMMENTARY DO COUNT HERE — unlike ordinary news screening, where tallies are dropped. For migration the aggregate IS the phenomenon: Rosstat figures, demographers' estimates, an official's admission of outflow, a year-on-year comparison are ALL relevant.
ALSO COUNTS: replacement inbound migration presented as a consequence of the outflow or of federal policy (labour migrants brought in to fill the vacated jobs), and the tension that follows.
DOES NOT COUNT: tourism; seasonal or rotational work (вахта) presented as ordinary employment; one person's relocation story with no policy cause; crime news about foreign migrants with no migration-policy dimension; the arrival of tourists or investors.

BORDERLINE RULE. K1 and K3 are narrow: when the federal addressee (K1) or the policy cause of the departure (K3) is absent, DROP. K2 is deliberately broad: when a federal actor has acted on the region, KEEP even if the item looks routine. When in doubt on K1 or K3 → drop; when in doubt on K2 → keep.

GEO RESOLUTION (region)
Set "region" to the single canonical region name the event is TIED TO, EXACTLY one of:
"Алтай", "Алтайський край", "Тива", "Хакасія", "Красноярський край", "Іркутська область",
"Кемеровська область", "Новосибірська область", "Омська область", "Томська область",
"Бурятія", "Саха (Якутія)", "Забайкальський край", "Камчатський край", "Приморський край",
"Хабаровський край", "Амурська область", "Магаданська область", "Сахалінська область",
"Єврейська АО", "Чукотський АО", "Ханти-Мансійський АО — Югра", "Ямало-Ненецький АО".
If relevant but tied to none of the monitored regions, set region to null.
Beware homonyms and neighbour confusions — verify the ACTUAL tie of the event, not the block it sits in:
«Саха» ≠ «Сахалін»; «Забайкалля» ≠ «Бурятія»; «Алтай» (республіка, Горно-Алтайськ) ≠ «Алтайський край»
(Барнаул); Комсомольськ-на-Амурі = «Хабаровський край», НЕ «Амурська область»; Норильськ = «Красноярський край», НЕ Ямало-Ненецький АО; Сєвєрськ (ЗАТО) = «Томська область», НЕ Красноярський край; Кузбас = «Кемеровська область»; Сургут / Нижньовартовськ / Нефтеюганськ = «Ханти-Мансійський АО — Югра», НЕ Тюменська область; Новий Уренгой / Ноябрськ / Надим = «Ямало-Ненецький АО»; «Ямало-Ненецький АО» (Салехард) ≠ «Ненецький АО» (Нар'ян-Мар, ПОЗА скоупом); сама Тюмень / Тобольськ = Тюменська область, яка ПОЗА скоупом (region null).
Anchors: region name / adjective; capitals & big cities —
Улан-Уде (Бурятія); Якутськ / Мирний / Нерюнгрі (Саха/Якутія); Кизил (Тива); Абакан / Черногорськ / Саяногорськ (Хакасія); Горно-Алтайськ (Алтай); Барнаул / Бійськ / Рубцовськ /
Новоалтайськ (Алтайський край); Чита / Краснокаменськ (Забайкальський край); Петропавловськ-Камчатський /
Вілючинськ (Камчатський край); Владивосток / Уссурійськ / Находка / Артем (Приморський край);
Хабаровськ / Комсомольськ-на-Амурі (Хабаровський край); Благовєщенськ / Свободний / Білогорськ
(Амурська область); Магадан / Ольський округ / Сусуман (Магаданська область); Южно-Сахалінськ /
Поронайськ / Курили (Сахалінська область); Біробіджан / Ленінське (Єврейська АО); Анадир / Білибіно /
Певек (Чукотський АО); Красноярськ / Норильськ / Ачинськ (Красноярський край); Іркутськ / Ангарськ / Братськ / Усть-Ілімськ (Іркутська область); Кемерово / Новокузнецьк / Прокоп'євськ / Междуреченськ (Кемеровська область); Новосибірськ / Бердськ / Куйбишев (Новосибірська область); Омськ / Тара (Омська область); Томськ / Сєвєрськ / Стрежевой (Томська область); Ханти-Мансійськ / Сургут / Нижньовартовськ / Нефтеюганськ / Когалим (Ханти-Мансійський АО — Югра); Салехард / Новий Уренгой / Ноябрськ / Надим / Муравленко (Ямало-Ненецький АО); regional organs, officials, enterprises and residents on the region's territory.

OUTPUT — respond with STRICT JSON only, no markdown fences, no commentary:
{"relevant": true|false,
 "reason": "<=15 words: which criterion (K1/K2/K3) and why, or why dropped",
 "signature": "WHO did WHAT WHERE — canonical fact fingerprint, in Ukrainian",
 "summary": "2-3 factual sentences for the event card, written in UKRAINIAN (translate the facts from the Russian source), no speculation",
 "region": "<one canonical region name above, or null>"}

Rules:
- "signature" and "summary" MUST be written in UKRAINIAN.
- "signature" stays stable across rewordings of the same fact (deduplication): actor, action, place;
  drop volatile numbers.
- If relevant=false: reason states why, signature and summary MUST be empty strings, region null.
- One news item = one event; duplicates are merged downstream — screen each item on its own.
"""

TAGGER_PROMPT = """ПРАВИЛА ТЕГУВАННЯ (лише для relevant=true):

"fed_criterion" — ОДНЕ АБО КІЛЬКА значень, суворо зі списку. Постав кожен критерій,
якому item реально відповідає (одна новина може бути і K1, і K2 — напр. протест проти
рішення федерального міністерства):
- K1_протест — колективна/публічна протестна дія з адресатом-федцентром (див. K1);
- K2_дії_федвлади — дія федерального актора, спрямована на суб'єкт (див. K2);
- K3_міграція — відтік/переїзд населення та його причини й наслідки (див. K3).

"fed_tone" — РІВНО ОДНЕ значення: як САМЕ ДЖЕРЕЛО подає подію (не твоя оцінка факту):
- тон_критичний — джерело показує шкоду, провал, тиск, невдоволення, іронізує над центром;
- тон_нейтральний — суха констатація без оцінки (типово для офіційних повідомлень);
- тон_схвальний — джерело подає дію центру як здобуток, турботу, допомогу.

"fed_importance" — РІВНО ОДНЕ значення:
- важливість_5 — системний рівень: масовий протест, рішення центру, що змінює статус чи
  бюджет суб'єкта, зміна глави регіону, підтверджений масовий відтік населення;
- важливість_4 — гучна подія широкого розголосу: помітний протест, кримінальна справа
  федеральних органів проти високопосадовця регіону, велике федеральне рішення;
- важливість_3 — помітна подія районного/міського рівня або окремий резонансний випадок;
- важливість_2 — локальна подія з обмеженим впливом (типова рутинна дія центру);
- важливість_1 — дрібна, але формально релевантна згадка.
"""


class Command(BaseCommand):
    help = "Створити/оновити дослідницьку задачу «федеральний тиск» (K1/K2/K3)"

    def add_arguments(self, parser):
        parser.add_argument("--from-task", required=True,
                            help="slug задачі, з якої копіюємо TELEGRAM-підписки")
        parser.add_argument("--slug", default=SLUG)
        parser.add_argument("--keep-prompts", action="store_true",
                            help="не перезаписувати промпти (зберегти ручні правки з адмінки)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        src_task = AnalysisTask.objects.filter(slug=opts["from_task"]).first()
        if not src_task:
            raise CommandError(f"Задачу-донора '{opts['from_task']}' не знайдено")

        tg_ids = list(SourceSubscription.objects
                      .filter(task=src_task, is_active=True, source__kind="telegram",
                              source__is_active=True)
                      .values_list("source_id", flat=True))
        if not tg_ids:
            raise CommandError(f"У задачі {src_task.slug} немає активних telegram-підписок")

        if opts["dry_run"]:
            self.stdout.write(
                f"[dry-run] створив би задачу '{opts['slug']}' (owner={src_task.owner_id}), "
                f"{len(TAG_CATS)} категорій тегів, {len(tg_ids)} telegram-підписок "
                f"з {src_task.slug}; промпти: скрін {len(SCREEN_PROMPT)} симв., "
                f"тегувальник {len(TAGGER_PROMPT)} симв.")
            return

        with transaction.atomic():
            task, created = AnalysisTask.objects.get_or_create(
                slug=opts["slug"],
                defaults=dict(
                    name=NAME,
                    pipeline=AnalysisTask.PIPELINE_INFOSPACE,
                    owner=src_task.owner,
                    is_active=True,
                ),
            )
            # Вікно свіжості 40 діб — інакше `_fanout` відсіє місячний бекфіл на вході;
            # retention 45 — інакше сирі пости зникнуть до того, як фільтр відтюнять.
            task.info_max_age_days = 40
            task.info_retention_days = 45
            task.info_screen_model = src_task.info_screen_model or ""
            task.llm_model = src_task.llm_model or ""
            task.review_enabled = False
            if not opts["keep_prompts"]:
                task.info_screen_prompt = SCREEN_PROMPT
                task.info_tagger_prompt = TAGGER_PROMPT
            task.save()

            n_tags = 0
            for key, label, order, values in TAG_CATS:
                cat, _ = TagCategory.objects.update_or_create(
                    key=key, defaults=dict(label=label, closed=True, order=order))
                task.tag_categories.add(cat)
                for v in values:
                    _, made = Tag.objects.get_or_create(name=v, category=key)
                    n_tags += int(made)

            existing = set(SourceSubscription.objects
                           .filter(task=task).values_list("source_id", flat=True))
            new_subs = [SourceSubscription(task=task, source_id=sid)
                        for sid in tg_ids if sid not in existing]
            SourceSubscription.objects.bulk_create(new_subs)

        self.stdout.write(self.style.SUCCESS(
            f"Задача {task.slug} (id={task.id}) {'створена' if created else 'оновлена'}: "
            f"{len(TAG_CATS)} категорій тегів (+{n_tags} нових тегів), "
            f"підписок telegram {len(tg_ids)} (+{len(new_subs)} нових), "
            f"промпти {'збережено' if opts['keep_prompts'] else 'перезаписано'}."))
        self.stdout.write(
            f"Далі: python manage.py infospace_backfill --task {task.slug} "
            f"--since <YYYY-MM-DD> --dry-run")
