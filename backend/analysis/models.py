"""
Конфігурований фреймворк аналізу Telegram → події.

Пайплайн:  AnalysisTask (конфігурація)
  collect (запит TeleZip) -> Post
  enrich (Telethon: надійна дата + метадані Channel)
  classify (LLM за схемою задачі) -> сирі поля
  normalize (вільний текст -> канонічне через таблиці аліасів: Tag, Region)
  dedup (попарно LLM у вікні) -> Event (M2M сторони, 1 подія <- N постів)

«Етнічні сутички 2025» — це ОДИН рядок AnalysisTask; ніщо тут не захардкоджено під неї.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.safestring import mark_safe


# ---------------------------------------------------------------------------
# Конфігурація задачі — параметризує весь пайплайн
# ---------------------------------------------------------------------------

def _default_prescreen_prompt():
    """Текст за замовчуванням для нових задач — щоб промпт було ВИДНО і можна
    було правити в адмінці, а не шукати у коді."""
    from analysis.pilot.prompts import PRESCREEN_SYSTEM_PROMPT_COMPACT
    return PRESCREEN_SYSTEM_PROMPT_COMPACT


def _default_tagger_prompt():
    from analysis.pilot.prompts import TAGGER_SYSTEM_PROMPT
    return TAGGER_SYSTEM_PROMPT


def _default_cluster_prompt():
    from analysis.services.research_stages import CLUSTER_PROMPT
    return CLUSTER_PROMPT


def _default_agent_review_prompt():
    from pathlib import Path
    import analysis.pilot as _pilot
    f = Path(_pilot.__file__).parent / "EVENT_REVIEW_PROMPT.md"
    return f.read_text() if f.exists() else ""


def _default_info_screen_prompt():
    from analysis.services.infospace.prompts import INFO_SCREEN_PROMPT
    return INFO_SCREEN_PROMPT


def _default_info_judge_prompt():
    from analysis.services.infospace.prompts import INFO_JUDGE_PROMPT
    return INFO_JUDGE_PROMPT


def _default_research_audit_prompt():
    """Дефолтний промпт агент-аудиту research-подій (рубрики етнічні C2-C4,
    економічні E1-E4, політичні P1-P3). Формат вердикту — як events-аудит,
    але правила специфічні для research-конвеєра (історичні граблі з
    docs/ethnic-events-pipeline.md, docs/econ-events-pipeline.md)."""
    return """Ти — строгий рев'юер бази подій тематичного дослідження (республіки РФ:
етнічна напруга, економічні конфлікти, політика). Кожна подія вже створена
механічним групуванням постів. Перечитай КОЖНУ і винеси вердикт: keep чи reject.

REJECT обов'язково в цих випадках:
  ✗ СТАРА РЕТРОСПЕКТИВА: текст лише ЗГАДУЄ давню історію/річницю/спогад без
    жодної СВІЖОЇ процесуальної дії у 2025-2026 (немає нового арешту, суду,
    протесту, заяви влади, розслідування саме зараз) — reject.
  ✗ ПАСТКА ДЕРЖПРОПАГАНДИ (C4): матеріал про «дружбу народів», «єдність
    багатонаціональної Росії», офіційні свята толерантності — це ПРОПАГАНДА,
    а не подія конфлікту/напруги; reject, якщо нема конкретного інциденту.
  ✗ НЕВІДПОВІДНІСТЬ РУБРИЦІ (misfit): подія не описує те, що заявлено в
    tags/rubric (напр. позначено як конфлікт, а текст — нейтральна новина без
    сторін і дії) — reject.
  ✗ НЕ-ПОДІЯ: думка/коментар/аналітика без конкретного місця+часу+дії.
  ✗ ДУБЛЬ: та сама подія вже є в батчі під меншим id.

KEEP лише якщо є конкретний інцидент (місце/час/дія), що реально відповідає
своїй рубриці, і (якщо рубрика етнічна/економічна/політична напруга) не є
просто цитуванням державної пропаганди про єдність.

ФОРМАТ ВІДПОВІДІ — строго валідний JSON без markdown, рівно стільки ж items:
{"items":[
  {"id":123,"verdict":"keep","reason":""},
  {"id":124,"verdict":"reject","reason":"стара ретроспектива без свіжої дії"},
  {"id":125,"verdict":"reject","reason":"держпропаганда єдності народів, не подія"},
  {"id":126,"verdict":"reject","reason":"не відповідає рубриці"}
]}
reason ≤120 знаків; для keep без зауважень — порожній рядок.
"""


class AnalysisTask(models.Model):
    name = models.CharField(max_length=200, verbose_name="Назва")
    slug = models.SlugField(unique=True, verbose_name="Ідентифікатор (slug)")
    description = models.TextField(blank=True, verbose_name="Опис")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="owned_tasks", verbose_name="Власник",
        help_text="Не-суперюзери бачать в адмінці лише свої задачі.")

    # Збір (TeleZip)
    telezip_query = models.TextField(
        verbose_name="Пошуковий запит TeleZip",
        help_text=mark_safe(
            'Запит у синтаксисі TeleZip (text=...). '
            '<a href="https://docs.google.com/document/d/'
            '1oKag8XfmpOnKapbkayRZzq8JHbpB8GaKvSgC1judnvg/edit?tab=t.0" '
            'target="_blank" rel="noopener">Документація TeleZip ↗</a>'
        ),
    )
    languages = models.JSONField(
        default=list, blank=True, verbose_name="Мови (фільтр пошуку TeleZip)",
        help_text='Збирати лише пости цими мовами, напр. ["ru"] — інакше в вибірку '
                  'лізуть інші мови. Порожньо: пошук подій — без фільтра; '
                  'моніторинг — автоматично "ru".',
    )
    search_posts = models.BooleanField(
        default=True, verbose_name="Шукати по постах",
        help_text="Включати пости каналів",
    )
    search_comments = models.BooleanField(
        default=False, verbose_name="Шукати по коментарях",
        help_text="Включати коментарі/повідомлення чатів",
    )
    drop_linked_comments = models.BooleanField(
        default=False, verbose_name="Відсівати linked-коментарі",
        help_text="TeleZip повертає коментарі із закритих груп-обговорень з "
                  "префіксом 'linked:' у назві каналу. Увімкни, щоб такі пости "
                  "не потрапляли в конвеєр подій (відсіюються на стадії precluster).",
    )
    min_channel_subscribers = models.PositiveIntegerField(
        default=0, verbose_name="Мін. підписників каналу",
        help_text="0 = без фільтра. Якщо >0 — пости з ЕНРІЧЕНИХ каналів, де підписників "
                  "менше за це число, відсіюються на precluster (не йдуть у classify/dedup). "
                  "Прибирає бот-ферму: одну історію, накручену по тисячах мікроканалів.",
    )
    collect_chunk_days = models.PositiveSmallIntegerField(
        default=1, verbose_name="Розмір чанка збору (днів)",
        help_text="Період дробиться на чанки для TeleZip (ліміт ~2 хв/запит). "
                  "Не впливає на вікно дедупу — воно працює на весь період.",
    )
    telezip_unique = models.BooleanField(
        default=False, verbose_name="TeleZip unique (згортати репости)",
        help_text="Якщо ВИМКНЕНО — збирати ВСІ репости (повне охоплення, але більше даних). "
                  "Якщо увімкнено — TeleZip віддає по одній копії (менше даних, занижене охоплення).",
    )

    # Класифікація (LLM) — схема/інструкції під конкретну задачу
    classify_system_prompt = models.TextField(
        verbose_name="Системний промпт класифікації",
        help_text="Системний промпт; має вимагати строгий JSON з полями задачі.",
    )

    # Параметри дедуплікації
    dedup_window_days = models.PositiveSmallIntegerField(
        default=3, verbose_name="Вікно дедупу (днів)",
    )
    dedup_pre_thresh = models.PositiveSmallIntegerField(
        default=82, verbose_name="Поріг попереднього злиття (%)",
        help_text="Fuzzy-поріг для дешевого злиття майже однакових репостів",
    )
    dedup_cand_thresh = models.PositiveSmallIntegerField(
        default=55, verbose_name="Поріг пар-кандидатів (%)",
        help_text="Fuzzy-поріг відбору пар на перевірку LLM",
    )

    llm_model = models.CharField(
        max_length=100, blank=True, verbose_name="Модель LLM",
        help_text="Перевизначає модель за замовчуванням",
    )

    # --- доменна конфігурація (робить пайплайн переюзабельним під інші задачі) ---
    geo_enabled = models.BooleanField(
        default=True, verbose_name="Геолокація (суб'єкт+місто)",
        help_text="Увімкнено: модель витягує з тексту суб'єкт РФ і місто (якщо в тексті "
                  "нема — береться «домашній» регіон каналу), сирі назви зводяться до "
                  "довідника суб'єктів. Вимкнено (моніторинг): регіон = регіон чату, "
                  "в якому написано коментар.",
    )
    tag_categories = models.ManyToManyField(
        "TagCategory", blank=True, related_name="tasks", verbose_name="Категорії тегів",
        help_text="Які категорії тегів класифікатор збирає з поста (схема промпта будується з них).",
    )
    dedup_judge_prompt = models.TextField(
        blank=True, verbose_name="Промпт дедуп-судді",
        help_text="Системний промпт LLM-судді «одна подія чи різні». Порожньо — дефолтний.",
    )
    generic_sides = models.JSONField(
        default=list, blank=True, verbose_name="Генеричні сторони",
        help_text="«Парасолькові» сторони, що НЕ вважаються спільним сигналом у дедупі "
                  '(напр. мігрант/місцевий). Порожньо — дефолтний набір.',
    )

    # --- аудит подій: два яруси ---
    # ярус 1 — АВТО-аудит воркером (дешева LLM, грубий перший прохід);
    # ярус 2 — АГЕНТ-аудит (гібрид: батчі -> Claude-агенти -> вердикти)
    review_enabled = models.BooleanField(
        default=False, verbose_name="Авто-аудит: перший прохід",
        help_text="Воркер проганяє готові події ДЕШЕВОЮ моделлю: грубий відсів "
                  "хибнопозитивів. Якість добиває агент-аудит (етап нижче).",
    )
    review_model = models.CharField(
        max_length=100, blank=True, default="google/gemini-2.5-flash",
        verbose_name="Авто-аудит: модель",
        help_text="Дешева LLM (OpenRouter) для першого проходу — напр. gemini-flash.",
    )
    review_prompt = models.TextField(
        blank=True, verbose_name="Авто-аудит: промпт",
        help_text="Системний промпт авто-аудитора. Порожньо — дефолтний.",
    )
    agent_review_prompt = models.TextField(
        blank=True, default=_default_agent_review_prompt,
        verbose_name="Агент-аудит: промпт",
        help_text="Зберігається в задачі й береться звідси при запуску аудиту. "
                  "Порожньо — стандартний EVENT_REVIEW_PROMPT.md (запасний варіант).",
    )

    # --- конфіг monitor-стадій (реюзабельність: усе редагується з адмінки, ---
    # --- згруповано по етапах у формі; порожнє поле = дефолт із коду) ---
    mon_min_len = models.PositiveSmallIntegerField(
        default=25, verbose_name="Фільтр: мін. довжина коментаря",
        help_text="Коротші повідомлення відсіюються як шум (емодзі, «+1»).",
    )
    mon_max_len = models.PositiveSmallIntegerField(
        default=600, verbose_name="Фільтр: макс. довжина",
        help_text="Довші — найімовірніше пости/репости каналу, не коментарі людей.",
    )
    prescreen_enabled = models.BooleanField(
        default=True, verbose_name="Прескрін: увімкнено",
        help_text="Дешевий «так/ні» відсів перед тегуванням. Він різав обсяг у 5-10 разів, "
                  "коли на вході був СУЦІЛЬНИЙ потік. При ВИБІРКОВОМУ зборі обсяг і так "
                  "малий, а recall прескріну ~85% — тобто кожен сьомий критичний коментар "
                  "мовчки стає «нерелевантним» і занижує чисельник метрики. "
                  "Для вибіркових задач вимикати.",
    )
    # --- конвеєр tgsearch: пошук у чатах через Telegram --------------------
    search_terms = models.TextField(
        blank=True, verbose_name="Слова пошуку (по одному в рядок)",
        help_text="Telegram НЕ вміє OR — кожне слово це окремий запит до кожного чату. "
                  "Тому сміттєві слова сюди класти дорого: вартість лінійна від їх "
                  "кількості (слів × чатів запитів на прогін).",
    )
    search_days = models.PositiveSmallIntegerField(
        default=7, verbose_name="Глибина пошуку, діб",
    )
    search_limit_per_term = models.PositiveSmallIntegerField(
        default=100, verbose_name="Стеля влучень на слово",
        help_text="У дуже жвавому чаті число влучень буде впертим у стелю — "
                  "це «≥ стелі», а не точна кількість.",
    )
    prescreen_model = models.CharField(
        max_length=100, blank=True, verbose_name="Прескрін: модель (OpenRouter)",
        help_text="Дешева модель для «так/ні» відсіву. Порожньо — дефолт із settings.",
    )
    prescreen_prompt = models.TextField(
        blank=True, default=_default_prescreen_prompt,
        verbose_name="Прескрін: системний промпт",
        help_text="Зберігається в задачі й береться звідси при запуску. "
                  "Порожньо — стандартний із коду (запасний варіант).",
    )
    tagger_prompt = models.TextField(
        blank=True, default=_default_tagger_prompt,
        verbose_name="Тегування: системний промпт агента",
        help_text="Зберігається в задачі; йде агентам у SYSTEM_PROMPT.md пачок. "
                  "Порожньо — стандартний із коду (запасний варіант).",
    )

    # --- групування дублів (research-конвеєр): 2 механічні пороги + LLM-крок ---
    dedup_group_days = models.PositiveSmallIntegerField(
        default=3, verbose_name="Дедуп: вікно склейки (днів)",
        help_text="Пости одного інциденту в межах ±N днів вважаються тим самим.")
    dedup_group_fuzz = models.PositiveSmallIntegerField(
        default=70, verbose_name="Дедуп: поріг схожості (%)",
        help_text="Схожість підсумків (0-100) для механічної склейки очевидних "
                  "дублів. Вище = обережніше (менше склеює).")
    dedup_llm_cluster = models.BooleanField(
        default=True, verbose_name="Дедуп: LLM-злиття інцидентів",
        help_text="Після механіки — агент зливає інциденти «одна подія різними "
                  "словами» (як історичний скрипт). Вимкни, щоб лишити лише "
                  "механіку (дешевше, але дробить дублі).")
    dedup_cluster_prompt = models.TextField(
        blank=True, default=_default_cluster_prompt,
        verbose_name="Дедуп: промпт LLM-злиття",
        help_text="Іде агенту-обʼєднувачу (SYSTEM_PROMPT_CLUSTER.md). Зберігається "
                  "в задачі; порожньо — стандартний із коду.")

    # --- агент-аудит подій research-конвеєра (опційна стадія після групування) ---
    research_audit_enabled = models.BooleanField(
        default=False, verbose_name="Агент-аудит подій",
        help_text="Після групування дублів у події запуск ставиться в «Чекає "
                  "агента»: Claude-агенти виносять keep/reject по кожній події, "
                  "ранер застосовує вердикти (reject → review_status=rejected).")
    research_audit_prompt = models.TextField(
        blank=True, default=_default_research_audit_prompt,
        verbose_name="Агент-аудит: промпт",
        help_text="Іде агентам у SYSTEM_PROMPT_AUDIT.md. Зберігається в задачі; "
                  "порожньо — стандартний із коду.")

    # --- конфіг infospace-стадій (моніторинг інформпростору; ---
    # --- див. docs/infospace-monitoring-pipeline.md) ---
    info_screen_model = models.CharField(
        max_length=100, blank=True, verbose_name="Скрін: модель (OpenRouter)",
        help_text="Дешева модель для фільтра релевантності + короткого опису. "
                  "Порожньо — дефолт із settings.",
    )
    info_screen_prompt = models.TextField(
        blank=True, default=_default_info_screen_prompt,
        verbose_name="Скрін: системний промпт",
        help_text="Критерії теми задачі; модель повертає relevant/signature/"
                  "summary/region/tags одним викликом. Порожньо — стандартний із коду.",
    )
    info_tagger_prompt = models.TextField(
        blank=True, verbose_name="Скрін: правила тегування",
        help_text="Додаткові правила для блоку tags (напр. критерії тегу «гучна "
                  "подія»). Додаються до скрін-промпту; схема категорій будується "
                  "з «Категорії тегів». Порожньо — лише підказки категорій.",
    )
    info_judge_prompt = models.TextField(
        blank=True, default=_default_info_judge_prompt,
        verbose_name="Зіставлення: промпт судді",
        help_text="«Той самий факт чи інший?» — attach/new + чи оновити опис. "
                  "Порожньо — стандартний із коду.",
    )
    info_max_age_days = models.PositiveSmallIntegerField(
        default=7, verbose_name="Свіжість: макс. вік новини (днів)",
        help_text="Елементи, старші за N діб (за датою публікації), НЕ потрапляють "
                  "у конвеєр — монітор бере лише свіже. Захищає і від хибної дати "
                  "extraction / архівних сторінок.",
    )
    info_match_window_hours = models.PositiveSmallIntegerField(
        default=24, verbose_name="Зіставлення: вікно збігу (год)",
        help_text="Ковзне вікно пошуку «такої самої події» відносно дати поста.",
    )
    info_update_summaries = models.BooleanField(
        default=True, verbose_name="Жива подія (оновлювати опис)",
        help_text="Суттєві доповнення з нових постів переписують опис події. "
                  "Вимкнено — опис фіксується з першого поста.",
    )
    info_retention_days = models.PositiveSmallIntegerField(
        default=2, verbose_name="Ретеншн сирих постів (днів)",
        help_text="Нерелевантні done-пости старші за N діб видаляються "
                  "(пости, приєднані до подій, не чіпаються).",
    )

    # --- вибір конвеєра: які stage-воркери обробляють пости задачі ---
    PIPELINE_EVENTS = "events"
    PIPELINE_MONITOR = "monitor"
    PIPELINE_RESEARCH = "research"
    PIPELINE_INFOSPACE = "infospace"
    PIPELINE_TGSEARCH = "tgsearch"
    PIPELINE_CHOICES = [
        (PIPELINE_EVENTS, "Події (enrich→precluster→classify→dedup)"),
        (PIPELINE_MONITOR, "Моніторинг думок (filter→prescreen→tag)"),
        (PIPELINE_RESEARCH, "Тематичне дослідження (канали→рубрики→агенти→дедуп)"),
        (PIPELINE_INFOSPACE, "Інформпростір (полінг джерел→скрін→жива подія)"),
        (PIPELINE_TGSEARCH, "Пошук у чатах через Telegram (пошук→фільтр→теги→подія)"),
    ]
    pipeline = models.CharField(
        max_length=12, choices=PIPELINE_CHOICES, default=PIPELINE_EVENTS,
        db_index=True, verbose_name="Конвеєр",
        help_text="events-воркери та monitor-воркери беруть лише «свої» задачі — "
                  "пост задачі ніколи не потрапить у чужу стадію.",
    )

    is_active = models.BooleanField(default=True, verbose_name="Активна")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Задача аналізу"
        verbose_name_plural = "Задачі аналізу"

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Кеш каналів (fallback регіону + майбутній аналіз каналів)
# ---------------------------------------------------------------------------

class Channel(models.Model):
    tg_id = models.BigIntegerField(null=True, blank=True, db_index=True, verbose_name="Telegram ID")
    username = models.CharField(max_length=128, blank=True, db_index=True, verbose_name="Юзернейм")
    title = models.CharField(max_length=512, blank=True, verbose_name="Назва")
    description = models.TextField(blank=True, verbose_name="Опис")
    subscribers = models.IntegerField(default=0, verbose_name="Підписники")
    language = models.CharField(max_length=16, blank=True, verbose_name="Мова")
    inferred_region = models.CharField(
        max_length=128, blank=True, verbose_name="Регіон (визначений)",
        help_text="Регіон, визначений із назви/опису каналу (fallback)",
    )
    is_channel = models.BooleanField(
        null=True, blank=True, verbose_name="Є каналом",
        help_text="False = чат/коментарі",
    )
    raw_meta = models.JSONField(default=dict, blank=True, verbose_name="Сирі метадані")
    enriched = models.BooleanField(default=False, verbose_name="Збагачено")
    fetched_at = models.DateTimeField(null=True, blank=True, verbose_name="Отримано")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    # --- Директорія каналів/чатів РФ (регіон + теми + аналіз) ---
    CHAT_TYPE_CHOICES = [
        ("channel", "Канал (мовлення)"),
        ("chat", "Чат/група (обговорення)"),
        ("discussion", "Група-обговорення каналу (linked)"),
        ("unknown", "Невідомо"),
    ]
    region_subject = models.ForeignKey(
        "Region", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="channels", verbose_name="Суб'єкт РФ (канонічний)",
    )
    chat_type = models.CharField(
        max_length=16, choices=CHAT_TYPE_CHOICES, blank=True, db_index=True,
        verbose_name="Тип",
    )
    topics = models.JSONField(
        default=list, blank=True, verbose_name="Теми",
        help_text="Список тем (новини, етнічне, політика, локал-чат, барахолка…)",
    )
    discusses_problems = models.BooleanField(
        null=True, blank=True, db_index=True, verbose_name="Обговорює проблеми РФ",
        help_text="Чи реально обговорюють суспільні/політичні/етнічні проблеми РФ",
    )
    directory_focus = models.CharField(
        max_length=300, blank=True, verbose_name="Фокус (1 речення)",
    )
    directory_meta = models.JSONField(
        default=dict, blank=True, verbose_name="Метадані класифікації",
        help_text="Сире рішення агента: confidence, reason, сирі region/topics.",
    )
    directory_classified_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Класифіковано (директорія)",
    )

    # --- Гео до рівня населеного пункту ------------------------------------
    settlement = models.CharField(
        max_length=160, blank=True, db_index=True, verbose_name="Населений пункт",
        help_text="Місто/райцентр, до якого прив'язаний канал. Той самий тип і назва, "
                  "що в Event.settlement і RegionAlias.settlement — канонізація спільна.",
    )

    # --- Коментарі: чи є де писати людям ------------------------------------
    comments_open = models.BooleanField(
        null=True, blank=True, db_index=True, verbose_name="Коментарі відкриті",
        help_text="Канал: має linked-групу обговорення. Чат: приймає повідомлення. "
                  "None = ще не перевіряли (перевіряє Telethon, не TGStat).",
    )
    linked_chat = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="linked_from", verbose_name="Група обговорення",
        help_text="Канал -> його linked-група (там і лежать коментарі). "
                  "Зворотний бік звʼязку (група -> чий це канал) — related_name linked_from.",
    )
    participants_visible = models.BooleanField(
        null=True, blank=True, verbose_name="Список учасників відкритий",
        help_text="ChannelFull.can_view_participants: чи можна прочитати склад учасників.",
    )

    # --- Активність (пише крок замірів, не збір) ----------------------------
    msgs_per_day = models.FloatField(
        null=True, blank=True, verbose_name="Повідомлень/добу (сирих)",
        help_text="З різниці id. УВАГА: рахує і автопересилки постів каналу.",
    )
    human_msgs_per_day = models.FloatField(
        null=True, blank=True, verbose_name="Людських повідомлень/добу",
        help_text="Без автопересилок каналу — головна метрика відбору джерела.",
    )
    last_post_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Останнє повідомлення",
    )
    activity_checked_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Активність міряли",
    )

    # --- Доступ --------------------------------------------------------------
    ACCESS_CHOICES = [
        ("public", "Публічний"), ("invite", "За інвайт-лінком"),
        ("closed", "Закритий"), ("dead", "Мертвий/видалений"),
    ]
    access = models.CharField(
        max_length=16, choices=ACCESS_CHOICES, blank=True, db_index=True,
        verbose_name="Доступ",
    )
    invite_hash = models.CharField(
        max_length=64, blank=True, verbose_name="Інвайт-лінк",
    )
    joined_by = models.ForeignKey(
        "accounts.TelegramAccount", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="joined_channels", verbose_name="Резолвив/вступав акаунт",
        help_text="Резолв кешується в сесії ТОГО акаунта, що резолвив, — читати чат "
                  "треба ним же.",
    )
    check_error = models.CharField(
        max_length=200, blank=True, verbose_name="Помилка перевірки",
        help_text="Чому не вдалося: приватний, бан, немає історії, мертвий юзернейм.",
    )

    class Meta:
        verbose_name = "Канал"
        verbose_name_plural = "Канали"
        constraints = [
            models.UniqueConstraint(fields=["tg_id"], name="uniq_channel_tgid",
                                    condition=models.Q(tg_id__isnull=False)),
        ]
        indexes = [
            models.Index(fields=["region_subject", "discusses_problems"]),
            models.Index(fields=["chat_type", "subscribers"]),
            models.Index(fields=["settlement", "comments_open"]),
        ]

    def __str__(self):
        return self.username or self.title or f"channel#{self.tg_id}"


# ---------------------------------------------------------------------------
# Керовані довідники (відкриті + авто-мапінг аліасів -> без дублів за сенсом)
# ---------------------------------------------------------------------------

class TagCategory(models.Model):
    """
    Реєстр категорій тегів (спільний для всіх задач). Задача обирає, які саме
    категорії збирати з поста. `closed` = канонізувати лише з сід-списку.
    """
    key = models.SlugField(max_length=32, unique=True, verbose_name="Ключ")
    label = models.CharField(max_length=80, verbose_name="Назва")
    closed = models.BooleanField(
        default=False, verbose_name="Закрита (лише сід-список)",
        help_text="Якщо так — теги цієї категорії беруться лише з сід-списку, нові не створюються.",
    )
    hint = models.CharField(
        max_length=300, blank=True, verbose_name="Підказка для промпта",
        help_text="Що писати в цю категорію (для відкритих). Порожньо — дефолтна підказка.",
    )
    order = models.PositiveSmallIntegerField(default=100, verbose_name="Порядок")

    class Meta:
        verbose_name = "Категорія тегів"
        verbose_name_plural = "Категорії тегів"
        ordering = ["order", "key"]

    def __str__(self):
        return self.label or self.key


class Tag(models.Model):
    """Канонічний тег (значення в межах категорії). Категорія — ключ TagCategory."""
    name = models.CharField(max_length=80, verbose_name="Назва (канонічна)")
    category = models.CharField(max_length=32, default="other", db_index=True,
                                verbose_name="Категорія (key)")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Тег"
        verbose_name_plural = "Теги"
        ordering = ["category", "name"]
        constraints = [
            models.UniqueConstraint(fields=["name", "category"], name="uniq_tag_name_category"),
        ]

    def __str__(self):
        return f"{self.name} ({self.category})"


class TagAlias(models.Model):
    """Зіставляє будь-який варіант вільного тексту -> канонічний Tag (ключ у нижньому регістрі)."""
    raw = models.CharField(max_length=120, unique=True, verbose_name="Варіант (аліас)")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="aliases", verbose_name="Тег")

    class Meta:
        verbose_name = "Аліас тега"
        verbose_name_plural = "Аліаси тегів"

    def __str__(self):
        return f"{self.raw} -> {self.tag_id}"


class Region(models.Model):
    """Канонічний суб'єкт РФ (область/республіка/край/місто федерального значення/АО)."""
    KIND_CHOICES = [
        ("республіка", "Республіка"), ("область", "Область"), ("край", "Край"),
        ("місто", "Місто федерального значення"), ("ао", "Автономний округ/область"),
    ]
    name = models.CharField(max_length=120, unique=True, verbose_name="Назва (канонічна)")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, blank=True, verbose_name="Тип")
    population = models.BigIntegerField(
        null=True, blank=True, verbose_name="Населення",
        help_text="К-сть мешканців (для нормалізації подій per-100k).",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Суб'єкт РФ"
        verbose_name_plural = "Суб'єкти РФ"
        ordering = ["name"]

    def __str__(self):
        return self.name


class RegionAlias(models.Model):
    """Зіставляє вільний текст регіону -> суб'єкт РФ (+населений пункт, якщо є)."""
    raw = models.CharField(max_length=200, unique=True, verbose_name="Варіант (аліас)")
    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name="aliases",
                               null=True, blank=True, verbose_name="Суб'єкт РФ")
    settlement = models.CharField(max_length=160, blank=True, verbose_name="Населений пункт")

    class Meta:
        verbose_name = "Аліас регіону"
        verbose_name_plural = "Аліаси регіонів"

    def __str__(self):
        return f"{self.raw} -> {self.region_id}"


# ---------------------------------------------------------------------------
# Дослідження (запуск пайплайну = знімок результатів)
# ---------------------------------------------------------------------------

class ResearchRun(models.Model):
    """
    Запит на збір за період («job»). Створює CollectChunk-и; воркери далі
    самі доводять пости до подій. Сама подія/пост належать ЗАДАЧІ (не job'у).
    """

    STATUS_CHOICES = [
        ("pending", "Очікує"),
        ("collecting", "Збір триває"),
        ("collected", "Збір завершено"),
        ("awaiting_agent", "Чекає агента (тегування)"),
        ("done", "Готово"),
        ("failed", "Помилка"),
        ("cancelled", "Скасовано"),
    ]

    task = models.ForeignKey(AnalysisTask, on_delete=models.CASCADE, related_name="runs", verbose_name="Задача")
    title = models.CharField(max_length=200, blank=True, verbose_name="Назва")
    date_from = models.DateField(verbose_name="Період від")
    date_to = models.DateField(verbose_name="Період до")

    chunk_days = models.PositiveSmallIntegerField(
        default=3, verbose_name="Розмір чанку (днів)",
        help_text="Скільки днів за один запит TeleZip (адаптивно зменшується при падіннях)",
    )
    min_chunk_days = models.PositiveSmallIntegerField(
        default=1, verbose_name="Мін. розмір чанку (днів)")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", verbose_name="Статус")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="Початок")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершення")

    params = models.JSONField(default=dict, blank=True, verbose_name="Знімок параметрів",
                              help_text="Копія конфігурації задачі на момент запуску")
    stats = models.JSONField(default=dict, blank=True, verbose_name="Агреговані результати",
                             help_text="Розбивки: по місяцях, регіонах, типах, сторонах")

    # денормалізовані лічильники для швидкого показу
    posts_collected = models.PositiveIntegerField(default=0, verbose_name="Зібрано постів")
    posts_relevant = models.PositiveIntegerField(default=0, verbose_name="Релевантних постів")
    events_total = models.PositiveIntegerField(default=0, verbose_name="Усього подій")
    events_corroborated = models.PositiveIntegerField(default=0, verbose_name="Підтверджених подій")

    error = models.TextField(blank=True, verbose_name="Помилка")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Збір (job)"
        verbose_name_plural = "Збори (jobs)"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or f"{self.task.slug} {self.date_from}…{self.date_to}"


class CollectChunk(models.Model):
    """Відрізок збору TeleZip (резюмабельність + адаптивний розмір)."""
    STATUS_CHOICES = [
        ("pending", "Очікує"),
        ("running", "Збирається"),
        ("done", "Зібрано"),
        ("failed", "Помилка"),
        ("split", "Розбито на менші"),
    ]

    task = models.ForeignKey(AnalysisTask, on_delete=models.CASCADE,
                             related_name="collect_chunks", verbose_name="Задача")
    job = models.ForeignKey(ResearchRun, on_delete=models.SET_NULL, null=True, blank=True,
                            related_name="chunks", verbose_name="Job")
    date_from = models.DateField(verbose_name="Від")
    date_to = models.DateField(verbose_name="До")

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending",
                              db_index=True, verbose_name="Статус")
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name="Спроб")
    posts_collected = models.PositiveIntegerField(default=0, verbose_name="Зібрано постів")
    locked_at = models.DateTimeField(null=True, blank=True, verbose_name="Захоплено")
    next_retry_at = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name="Наступна спроба не раніше",
        help_text="Експоненційний backoff для транзієнтних помилок мережі/TeleZip",
    )
    error = models.TextField(blank=True, verbose_name="Помилка")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершено")

    class Meta:
        verbose_name = "Чанк збору"
        verbose_name_plural = "Чанки збору"
        ordering = ["date_from"]
        indexes = [
            models.Index(fields=["task", "status"]),
            models.Index(fields=["task", "date_from", "date_to"]),
        ]

    def __str__(self):
        return f"{self.task.slug} {self.date_from}…{self.date_to} [{self.status}]"


# ---------------------------------------------------------------------------
# Пости та події
# ---------------------------------------------------------------------------

class MonitorSample(models.Model):
    """
    Вікно вибіркового збору: один чат × один період.

    Збираємо не суцільно, а ВИПАДКОВОЮ вибіркою id повідомлень у межах періоду.
    Цей рядок — паспорт вибірки: межі id, скільки id запитали, скільки повернулось
    і скільки з них із текстом. З нього рахується ЗНАМЕННИК метрики:

        оцінка обсягу чату за період = (id_hi - id_lo) × (n_text / n_requested)

    (частина id витрачається на службові події, видалені й медіа без тексту, тому
    сирий діапазон id — лише верхня межа). Без цього рядка вибірка невідтворювана
    і частку порахувати неможливо.
    """
    task = models.ForeignKey(AnalysisTask, on_delete=models.CASCADE,
                             related_name="samples", verbose_name="Задача")
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE,
                                related_name="samples", verbose_name="Чат")
    period_start = models.DateField(verbose_name="Початок періоду")
    period_end = models.DateField(verbose_name="Кінець періоду")
    id_lo = models.BigIntegerField(verbose_name="id на початку періоду")
    id_hi = models.BigIntegerField(verbose_name="id у кінці періоду")
    n_requested = models.PositiveIntegerField(default=0, verbose_name="Запитано id")
    n_returned = models.PositiveIntegerField(default=0, verbose_name="Повернуто повідомлень")
    n_text = models.PositiveIntegerField(default=0, verbose_name="З них із текстом")
    n_user = models.PositiveIntegerField(
        default=0, verbose_name="З них написані людьми",
        help_text="Решта — автопересилки постів каналу в групу обговорень (анкери, "
                  "під якими пишуть коментарі). У групі новинного каналу вони можуть "
                  "давати 99% потоку, тому «повідомлень на добу» без цієї поправки "
                  "міряє активність КАНАЛУ, а не людей.",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Вибірка (вікно)"
        verbose_name_plural = "Вибірки (вікна)"
        ordering = ["-period_start", "channel_id"]
        constraints = [
            models.UniqueConstraint(fields=["task", "channel", "period_start"],
                                    name="uniq_sample_task_channel_period"),
        ]

    @property
    def span(self) -> int:
        """Сирий діапазон id — верхня межа обсягу за період."""
        return max(0, self.id_hi - self.id_lo)

    @property
    def estimated_total(self) -> int:
        """Оцінка реального числа текстових повідомлень за період (знаменник)."""
        if not self.n_requested:
            return 0
        return int(round(self.span * self.n_text / self.n_requested))

    @property
    def estimated_user_total(self) -> int:
        """Оцінка числа повідомлень ВІД ЛЮДЕЙ — саме він знаменник метрики."""
        if not self.n_requested:
            return 0
        return int(round(self.span * self.n_user / self.n_requested))

    @property
    def people_rate(self) -> float:
        """Частка живих людей у потоці чату (0..1). Низька = це не чат, а рупор."""
        return (self.n_user / self.n_text) if self.n_text else 0.0

    def __str__(self):
        return f"{self.channel} {self.period_start:%Y-%m}: {self.n_text}/{self.n_requested}"


class Post(models.Model):
    # конвеєр стадій (claim-based черга, кожен воркер дивиться «свій» статус)
    STAGE_COLLECTED = "collected"
    STAGE_ENRICHED = "enriched"
    STAGE_PRECLUSTERED = "preclustered"
    STAGE_CLASSIFIED = "classified"
    STAGE_DEDUPED = "deduped"
    STAGE_DONE = "done"
    STAGE_FAILED = "failed"
    # opinion-monitor конвеєр (pipeline="monitor"): окремий префікс стадій,
    # щоб event-воркери (enrich/precluster/...) ніколи не захоплювали ці пости
    STAGE_MON_COLLECTED = "mon_collected"
    STAGE_MON_FILTERED = "mon_filtered"
    STAGE_MON_PRESCREENED = "mon_prescreened"
    # infospace-конвеєр (pipeline="infospace"): полінг джерел (Source), скрін,
    # зіставлення з живими подіями — docs/infospace-monitoring-pipeline.md
    STAGE_TGS_COLLECTED = "tgs_collected"
    STAGE_TGS_SCREENED = "tgs_screened"
    STAGE_TGS_TAGGED = "tgs_tagged"
    STAGE_INFO_COLLECTED = "info_collected"
    STAGE_INFO_SCREENED = "info_screened"
    STAGE_CHOICES = [
        (STAGE_COLLECTED, "Зібрано"),
        (STAGE_ENRICHED, "Збагачено"),
        (STAGE_PRECLUSTERED, "Прекластеризовано"),
        (STAGE_CLASSIFIED, "Класифіковано"),
        (STAGE_DEDUPED, "Дедупльовано"),
        (STAGE_MON_COLLECTED, "Монітор: зібрано"),
        (STAGE_MON_FILTERED, "Монітор: відфільтровано"),
        (STAGE_MON_PRESCREENED, "Монітор: прескрін+"),
        (STAGE_TGS_COLLECTED, "TG-пошук: знайдено"),
        (STAGE_TGS_SCREENED, "TG-пошук: релевантне"),
        (STAGE_TGS_TAGGED, "TG-пошук: протеговано"),
        (STAGE_INFO_COLLECTED, "Інформпростір: зібрано"),
        (STAGE_INFO_SCREENED, "Інформпростір: скрін+"),
        (STAGE_DONE, "Готово"),
        (STAGE_FAILED, "Помилка"),
    ]

    task = models.ForeignKey(AnalysisTask, on_delete=models.CASCADE, related_name="posts", verbose_name="Задача")
    stage = models.CharField(
        max_length=16, choices=STAGE_CHOICES, default=STAGE_COLLECTED, db_index=True,
        verbose_name="Стадія конвеєра",
    )
    stage_locked_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Захоплено воркером",
        help_text="Час claim'у; звільняється після успіху або таймауту",
    )
    stage_attempts = models.PositiveIntegerField(default=0, verbose_name="Спроб стадії")
    stage_error = models.TextField(blank=True, verbose_name="Помилка стадії")

    url = models.URLField(max_length=500, verbose_name="Посилання")
    channel = models.ForeignKey(
        Channel, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="posts", verbose_name="Канал",
    )
    channel_name = models.CharField(max_length=128, blank=True, verbose_name="Назва каналу")
    source = models.ForeignKey(
        "Source", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="posts", verbose_name="Джерело (інформпростір)",
        help_text="Звідки зібрано пост у infospace-конвеєрі; для TeleZip-конвеєрів NULL.",
    )
    title = models.CharField(
        max_length=500, blank=True, db_default="", verbose_name="Заголовок",
        help_text="Заголовок статті/RSS-item (для Telegram порожній).",
    )  # db_default: старий код воркерів (INSERT без title) не падає у вікні
       # «міграцію застосовано, контейнери ще не перезапущені» (граблі проєкту).
    region_subject = models.ForeignKey(
        "Region", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="posts", verbose_name="Суб'єкт РФ (денормалізовано)",
        help_text="Копія channel.region_subject для прямого GROUP BY і per-100k без JOIN "
                  "(спільний аналітичний контракт подій і критики — див. services/metrics.py).",
    )
    posted_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Дата публікації",
        help_text="Надійна дата (Telethon/TeleZip, UTC)",
    )
    telezip_date = models.DateTimeField(null=True, blank=True, verbose_name="Дата TeleZip")
    text = models.TextField(blank=True, verbose_name="Текст")
    content_hash = models.CharField(max_length=80, blank=True, db_index=True, verbose_name="Хеш контенту")
    telezip_mid = models.BigIntegerField(null=True, blank=True, verbose_name="TeleZip MID")

    # --- opinion-monitor поля (для коментарів-думок, не подій) ---
    author_name = models.CharField(
        max_length=128, blank=True, verbose_name="Автор (FromUserName)",
        help_text="Юзернейм або відображене ім'я автора коментаря",
    )
    author_tg_id = models.BigIntegerField(
        null=True, blank=True, db_index=True, verbose_name="Автор tg_id",
        help_text="FromUserId з TeleZip — для дедупу один-автор-у-багатьох-чатах",
    )
    reply_to_msg = models.BigIntegerField(
        null=True, blank=True, verbose_name="ReplyTo msg_id",
        help_text="Якщо це коментар-відповідь у linked-discussion",
    )
    also_in_chats = models.JSONField(
        default=list, blank=True, verbose_name="Також у чатах",
        help_text="Список username чатів, де той самий автор написав ідентичний текст "
                  "(збираємо як 1 Post замість N).",
    )
    is_channel_repost = models.BooleanField(
        default=False, db_index=True, verbose_name="Репост каналу в чаті",
        help_text="True = це повідомлення з КАНАЛУ (авто-форвард linked-каналу або "
                  "переслане), а не оригінальна репліка учасника. ІГНОРУВАТИ при аналізі "
                  "активності чату. Визначається збігом content_hash з постом каналу.",
    )
    tags = models.ManyToManyField(
        Tag, blank=True, related_name="posts", verbose_name="Теги",
        help_text="Теги opinion/topic/criticism_target. Заповнюються LLM-тегувальником.",
    )

    dedup_group = models.BigIntegerField(
        null=True, blank=True, db_index=True, verbose_name="Група дедупу",
        help_text="ID кластера після hash+fuzzy преклстеру (корінь групи)",
    )
    is_classified = models.BooleanField(default=False, verbose_name="Класифіковано")
    is_relevant = models.BooleanField(null=True, blank=True, verbose_name="Релевантний")
    classification = models.JSONField(default=dict, blank=True, verbose_name="Результат класифікації")
    date_enriched = models.BooleanField(default=False, verbose_name="Дату збагачено")

    event = models.ForeignKey(
        "Event", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="posts", verbose_name="Подія",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Пост"
        verbose_name_plural = "Пости"
        unique_together = [["task", "url"]]
        ordering = ["posted_at"]
        indexes = [
            models.Index(fields=["task", "stage"]),
            models.Index(fields=["task", "stage", "posted_at"]),
            # Covering index for the admin charts GROUP BY (filtered by task+stage,
            # bucketed by posted_at, grouped by channel) — lets it run index-only.
            models.Index(fields=["task", "stage", "posted_at", "channel"]),
        ]

    def __str__(self):
        return self.url


class Event(models.Model):
    """Дедуплікований реальний інцидент (1 подія <- N постів)."""
    task = models.ForeignKey(AnalysisTask, on_delete=models.CASCADE, related_name="events", verbose_name="Задача")
    event_date = models.DateField(
        null=True, blank=True, verbose_name="Дата події",
        help_text="З найранішого посту (дата публікації)",
    )
    region = models.CharField(max_length=128, blank=True, verbose_name="Регіон (сирий)")
    region_subject = models.ForeignKey(
        Region, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="events", verbose_name="Суб'єкт РФ",
    )
    settlement = models.CharField(max_length=160, blank=True, verbose_name="Населений пункт")
    tags = models.ManyToManyField(Tag, blank=True, related_name="events", verbose_name="Теги")
    # Сторони конфлікту (нападник/жертва) живуть у `tags` під ЗАКРИТИМИ
    # категоріями attacker_nationality / victim_nationality (словник дзеркалить
    # nationality). Колишні окремі M2M attacker_tags/victim_tags видалені —
    # дані перенесені в tags міграційним скриптом 2026-06-12.
    summary = models.TextField(blank=True, verbose_name="Опис")
    post_count = models.PositiveIntegerField(default=0, verbose_name="Кількість постів")
    channel_count = models.PositiveIntegerField(
        default=0, db_index=True, verbose_name="Кількість каналів",
        help_text="Скільки УНІКАЛЬНИХ каналів опублікували цю подію.",
    )
    reach = models.BigIntegerField(
        default=0, verbose_name="Охоплення",
        help_text="Сумарна к-сть підписників унікальних каналів події",
    )
    last_post_at = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name="Останній пост",
        help_text="Час останнього приєднаного поста. Заповнює ЛИШЕ infospace-"
                  "конвеєр (ковзне вікно збігу + сортування «живих» сюжетів); "
                  "інші конвеєри не чіпають.",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    # --- фінальний AI-аудит (дорожча модель) ---
    REVIEW_PENDING = "pending"
    REVIEW_APPROVED = "approved"
    REVIEW_REJECTED = "rejected"
    REVIEW_CHOICES = [
        (REVIEW_PENDING, "Очікує аудиту"),
        (REVIEW_APPROVED, "Схвалено"),
        (REVIEW_REJECTED, "Відхилено (буде видалено)"),
    ]
    review_status = models.CharField(
        max_length=12, choices=REVIEW_CHOICES, default=REVIEW_PENDING, db_index=True,
        verbose_name="Статус аудиту",
    )
    review_notes = models.TextField(blank=True, verbose_name="Нотатки аудиту")
    review_locked_at = models.DateTimeField(null=True, blank=True, verbose_name="Заблоковано аудитом о")
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="Проаудитовано о")

    class Meta:
        verbose_name = "Подія"
        verbose_name_plural = "Події"
        ordering = ["event_date"]
        indexes = [models.Index(fields=["task", "event_date"]),
                   models.Index(fields=["task", "review_status"])]

    def __str__(self):
        return f"{self.event_date} {self.region}: {self.summary[:60]}"


# ---------------------------------------------------------------------------
# Opinion-monitor: whitelist чатів для регіонального моніторингу
# ---------------------------------------------------------------------------

class MonitorChat(models.Model):
    """
    Чат у whitelist моніторингу опінії. Один AnalysisTask = один регіональний
    моніторинг; багато MonitorChat = список чатів, які ми збираємо для цього
    моніторингу.

    Замість YAML-конфігу, тримаємо це у БД щоб можна було редагувати з адмінки
    та бачити інлайн на сторінці самого AnalysisTask.
    """
    task = models.ForeignKey(
        AnalysisTask, on_delete=models.CASCADE, related_name="monitor_chats",
        verbose_name="Задача моніторингу",
    )
    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, related_name="enrolled_in",
        verbose_name="Чат",
    )
    is_active = models.BooleanField(
        default=True, db_index=True, verbose_name="Активний",
        help_text="Зніми галочку щоб виключити чат з наступного збору, "
                  "не видаляючи історичні дані.",
    )
    is_critical_source = models.BooleanField(
        default=False, db_index=True, verbose_name="Критичне джерело",
        help_text="Особливо важливий чат — пріоритет у звітах.",
    )
    tg_account = models.ForeignKey(
        "accounts.TelegramAccount", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="monitor_chats", verbose_name="Акаунт збору",
        help_text="Яким акаунтом читаємо цей чат. Резолв юзернейма має добовий ліміт "
                  "(~200/акаунт) і кешується В СЕСІЇ того акаунта, що резолвив, — тож "
                  "чат треба читати ТИМ САМИМ акаунтом, інакше резолв платиться заново.",
    )
    priority = models.PositiveSmallIntegerField(
        default=100, verbose_name="Пріоритет",
        help_text="Менше = вище у списку. Для сортування при показі.",
    )
    notes = models.TextField(blank=True, verbose_name="Нотатки")
    last_searched_at = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name="Останній TG-пошук",
        help_text="Конвеєр tgsearch: коли цей чат востаннє обшукували. "
                  "Порожньо = ще жодного разу.",
    )
    added_by = models.CharField(
        max_length=80, blank=True, verbose_name="Хто додав",
        help_text="Хто/коли додав чат у whitelist (ручний рядок).",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Чат моніторингу"
        verbose_name_plural = "Чати моніторингу"
        unique_together = [["task", "channel"]]
        ordering = ["task", "priority", "channel__username"]
        indexes = [models.Index(fields=["task", "is_active"])]

    def __str__(self):
        ch = self.channel
        u = (ch.username or "") if ch else ""
        return f"{self.task.slug}: @{u or 'no-username'}"


# ---------------------------------------------------------------------------
# Infospace: джерела моніторингу інформпростору (pipeline="infospace")
# docs/infospace-monitoring-pipeline.md
# ---------------------------------------------------------------------------

class Source(models.Model):
    """Джерело інформації в глобальному довіднику (Telegram-канал через акаунт,
    RSS-стрічка, сайт зі скрапінгом; згодом VK).

    До задач підключається через SourceSubscription (аналог Channel↔MonitorChat).
    Розклад/health/watermark пише ЛИШЕ стадія info_collect; руками з адмінки
    правлять тільки конфігурацію (kind/url/config/інтервал/актив).
    """
    KIND_TELEGRAM = "telegram"
    KIND_RSS = "rss"
    KIND_WEB = "web"
    KIND_VK = "vk"
    KIND_CHOICES = [
        (KIND_TELEGRAM, "Telegram-канал (акаунт)"),
        (KIND_RSS, "RSS-стрічка"),
        (KIND_WEB, "Сайт (скрапінг)"),
        (KIND_VK, "VK (згодом)"),
    ]

    kind = models.CharField(max_length=12, choices=KIND_CHOICES, db_index=True,
                            verbose_name="Тип")
    name = models.CharField(max_length=200, verbose_name="Назва")
    url = models.CharField(
        max_length=500, verbose_name="Ідентифікатор (URL)",
        help_text="tg: @username або t.me/…; rss: URL стрічки; "
                  "web: URL лістинг-сторінки розділу.",
    )
    region_subject = models.ForeignKey(
        "Region", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sources", verbose_name="Суб'єкт РФ",
        help_text="Регіон джерела; денормалізується в Post при вставці (per-100k).",
    )
    language = models.CharField(max_length=16, blank=True, verbose_name="Мова")

    # web-скрапінг
    scraper_key = models.CharField(
        max_length=50, blank=True, verbose_name="Кастомний скрапер",
        help_text="Ключ у реєстрі SCRAPERS (infospace/scrapers.py). "
                  "Порожньо = автоекстракція (trafilatura) / селектори з config.",
    )
    config = models.JSONField(
        default=dict, blank=True, verbose_name="Конфігурація",
        help_text="Селектори discovery/extraction, max_items, backfill_limit, "
                  "full_text, headers… — див. док конвеєра.",
    )

    # telegram
    tg_account = models.ForeignKey(
        "accounts.TelegramAccount", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="sources",
        verbose_name="Telegram-акаунт",
        help_text="Яким акаунтом полити цей канал. Порожньо — перший авторизований "
                  "(ротація по пулу — Phase 4).",
    )

    # розклад і health (пише лише worker info-collect)
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Активне")
    poll_interval_sec = models.PositiveIntegerField(
        default=600, verbose_name="Інтервал полінгу (сек)",
    )
    next_poll_at = models.DateTimeField(
        default=timezone.now, db_index=True, verbose_name="Наступний полінг",
        help_text="Черга полінгу; бекоф при збоях. «Опитати зараз» = поставити now.",
    )
    locked_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Захоплено воркером",
        help_text="Час claim'у; звільняється після проходу або таймауту.",
    )
    poll_cursor = models.JSONField(
        default=dict, blank=True, verbose_name="Курсор збору (докуди прочитано)",
        help_text="Позиція «докуди вже зібрано» для цього джерела, щоб брати лише "
                  "нове: Telegram — last_msg_id; RSS — seen_ids+etag; web — seen_ids. "
                  "Очистити (→ порожньо) = перечитати заново (backfill).",
    )
    last_ok_at = models.DateTimeField(null=True, blank=True, verbose_name="Останній успіх")
    last_error = models.TextField(blank=True, verbose_name="Остання помилка")
    consecutive_failures = models.PositiveSmallIntegerField(
        default=0, verbose_name="Збоїв поспіль",
    )

    # --- самоперевірка якості (info_healthcheck; ловить ТИХИЙ злам скрапера:
    # --- «успіх без користі», який consecutive_failures не бачить, бо не виняток) ---
    quality_ok = models.BooleanField(
        default=True, db_index=True, verbose_name="Якість ок",
        help_text="False = 🟡 підозра: dry-run дав 0 елементів або порожній текст "
                  "(сайт переверстали / селектор зламано). Ставить info_healthcheck.",
    )
    quality_note = models.CharField(
        max_length=200, blank=True, verbose_name="Нотатка якості",
        help_text="Чому 🟡 підозра (напр. «discovery: 0 лінків»).",
    )
    last_healthcheck_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Остання самоперевірка",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Джерело (інформпростір)"
        verbose_name_plural = "Джерела (інформпростір)"
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "url"], name="uniq_source_kind_url"),
        ]
        indexes = [
            models.Index(fields=["is_active", "next_poll_at"]),
        ]

    def __str__(self):
        return f"{self.name} [{self.kind}]"


class SourceSubscription(models.Model):
    """Підписка задачі на джерело (дзеркало MonitorChat для infospace).

    Пост створюється окремо на кожну підписану задачу — unique(task, url)
    не порушується; у кожної теми свій скрін і свої події.
    """
    task = models.ForeignKey(
        AnalysisTask, on_delete=models.CASCADE, related_name="source_subscriptions",
        verbose_name="Задача",
    )
    source = models.ForeignKey(
        Source, on_delete=models.CASCADE, related_name="subscriptions",
        verbose_name="Джерело",
    )
    is_active = models.BooleanField(
        default=True, db_index=True, verbose_name="Активна",
        help_text="Зніми галочку, щоб виключити джерело з наступних зборів "
                  "цієї задачі, не видаляючи історичні дані.",
    )
    priority = models.PositiveSmallIntegerField(
        default=100, verbose_name="Пріоритет",
        help_text="Менше = вище у списку.",
    )
    notes = models.TextField(blank=True, verbose_name="Нотатки")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Підписка на джерело"
        verbose_name_plural = "Підписки на джерела"
        unique_together = [["task", "source"]]
        ordering = ["task", "priority", "source__name"]
        indexes = [models.Index(fields=["task", "is_active"])]

    def __str__(self):
        return f"{self.task.slug}: {self.source}"


class ResearchRubric(models.Model):
    """Рубрика тематичного дослідження (конвеєр research): «що шукаємо».

    Історично рубрики жили в _dir-скриптах (C2-C4/E1-E4/P1-P3): AND-набір
    ключових слів відбирає кандидатів із сирого потоку каналів, агенти
    класифікують, збіги стають подіями з тегом рубрики."""
    task = models.ForeignKey(AnalysisTask, on_delete=models.CASCADE,
                             related_name="rubrics", verbose_name="Задача")
    key = models.SlugField(verbose_name="Ключ", blank=True,
        help_text="Технічний ідентифікатор рубрики; авто з тегу події, якщо порожньо.")
    name = models.CharField(max_length=200, verbose_name="Назва", blank=True)
    tag_category = models.CharField(
        max_length=50, verbose_name="Категорія тегу",
        help_text="Куди класти тег події (напр. econ_event).")
    tag_name = models.CharField(
        max_length=200, verbose_name="Тег події",
        help_text="Канонічна назва тегу рубрики (get_or_create у цій категорії).")
    keywords = models.JSONField(
        default=list, verbose_name="Ключові слова (АБО-список І-груп)",
        help_text='Список РЕГУЛЯРОК (ТЕМА ∧ ДІЯ): кандидат, якщо КОЖНА збіглась. '
                  'Напр. ["коррупц|взятк|откат", "задержа|арест|обыск"].')
    extra_prompt = models.TextField(
        blank=True, verbose_name="Доповнення промпту",
        help_text="Специфічні правила рубрики, додаються до промпту агента.")
    is_active = models.BooleanField(default=True, verbose_name="Активна")
    order = models.PositiveSmallIntegerField(default=0, verbose_name="Порядок")

    class Meta:
        verbose_name = "Рубрика дослідження"
        verbose_name_plural = "Рубрики дослідження"
        unique_together = [("task", "key")]
        ordering = ["order", "key"]

    def save(self, *args, **kwargs):
        # key/name прибрані з форми — виводимо з тегу події, щоб конвеєр
        # (агентні вердикти + групування спираються на key) не зламався
        if not self.key and self.tag_name:
            from django.utils.text import slugify
            self.key = slugify(self.tag_name, allow_unicode=True)[:50] or self.tag_name[:50]
        if not self.name:
            self.name = self.tag_name
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.key or self.tag_name


class TelezipSlot(models.Model):
    """Cross-process concurrency gate for TeleZip.

    N rows == N GLOBAL slots. A caller leases a free (or lease-expired) row for the
    duration of one /Find request, then releases it. Replaces the per-process
    asyncio.Semaphore so the concurrency cap holds across ALL worker processes
    (the per-process semaphore let every extra process add its own 2 slots, which
    is what tripped TeleZip 429s). Crash-safe: a dead holder's lease simply expires.
    """
    slot = models.IntegerField(unique=True)
    leased_until = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "analysis_telezip_slot"
        verbose_name = "TeleZip slot"
        verbose_name_plural = "TeleZip slots"

    def __str__(self):
        return f"slot {self.slot} (until {self.leased_until})"


class ChannelDailyStat(models.Model):
    """Денний агрегат повідомлень по (задача, канал, день).

    Зберігає знаменник «усіх повідомлень» ПІСЛЯ видалення сирих не-релевантних
    постів: % критики = relevant/total лишається рахованим на будь-якому зрізі
    (канал→регіон через Channel.region_subject; день→тиждень/місяць/рік).
    """
    task = models.ForeignKey("AnalysisTask", on_delete=models.CASCADE,
                             related_name="daily_stats")
    channel = models.ForeignKey("Channel", on_delete=models.CASCADE,
                                related_name="daily_stats")
    date = models.DateField(db_index=True, verbose_name="День")
    total = models.IntegerField(default=0, verbose_name="Усіх повідомлень (із зібраних постів)")
    relevant = models.IntegerField(default=0, verbose_name="Критика")
    reposts = models.IntegerField(default=0, verbose_name="Авто-репости каналу")
    # Авторитетна кількість УСІХ повідомлень за день із TeleZip (не з наших
    # зібраних/відфільтрованих постів). Знаменник «% критики» бере її, бо `total`
    # для деяких періодів = лише відфільтровані/кандидати, не всі повідомлення.
    telezip_total = models.IntegerField(null=True, blank=True,
                                        verbose_name="Усіх повідомлень (TeleZip, авторитетно)")

    class Meta:
        verbose_name = "Денна статистика каналу"
        verbose_name_plural = "Денні статистики каналів"
        constraints = [
            models.UniqueConstraint(fields=["task", "channel", "date"],
                                    name="uniq_chan_daily_stat"),
        ]
        indexes = [
            models.Index(fields=["task", "date"]),
            models.Index(fields=["channel", "date"]),
        ]

    def __str__(self):
        return f"{self.task_id}/{self.channel_id} {self.date}: {self.relevant}/{self.total}"


class Setting(models.Model):
    """Загальна key-value таблиця налаштувань (промпти, тексти, прапорці), які має
    правити оператор БЕЗ деплою. Читається кодом через `Setting.get(key, default)`;
    порожнє значення = дефолт із коду. Сюди поступово виносимо подібний конфіг."""
    key = models.SlugField(max_length=64, unique=True, verbose_name="Ключ")
    value = models.TextField(blank=True, verbose_name="Значення")
    description = models.CharField(max_length=300, blank=True, verbose_name="Опис")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Налаштування"
        verbose_name_plural = "Налаштування"
        ordering = ["key"]

    def __str__(self):
        return self.key

    @classmethod
    def get(cls, key, default=""):
        """Значення налаштування, або default якщо рядка нема / значення порожнє."""
        row = cls.objects.filter(key=key).first()
        val = (row.value or "").strip() if row else ""
        return val or default


# ---------------------------------------------------------------------------
# Publish-конвеєр: відбір approved-подій → AI-фільтр+рерайт → пост у Telegram
# ---------------------------------------------------------------------------

class PublishConfig(models.Model):
    """Операторський профіль публікації (редагується в адмінці).

    Відбирає approved-Event ДЗЕРКАЛОМ фасетів changelist (задача/теги/регіон),
    прогонить кожну через AI (фільтр yes/no + рерайт у пост) і публікує в один
    Telegram-канал через Bot API. Один рядок = один канал/профіль; кілька
    профілів можуть публікувати різні зрізи в різні канали.

    Claim/стан/аудит публікації живе в [[PublishedEvent]] (unique per config+event),
    тож повторна публікація тієї самої події виключена. Воркер: стадія `publish`
    (services/publish/stages.py, TASKLESS — ітерує активні профілі)."""

    name = models.CharField(max_length=120, verbose_name="Назва профілю")
    is_active = models.BooleanField(default=False, verbose_name="Активний")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="owned_publish_configs", verbose_name="Власник",
        help_text="Не-суперюзери бачать в адмінці лише свої профілі.")

    # --- відбір подій (дзеркало фасетів changelist подій) ---
    task = models.ForeignKey(
        AnalysisTask, on_delete=models.CASCADE, related_name="publish_configs",
        null=True, blank=True, verbose_name="Задача (збір)",
        help_text="Порожньо = події всіх задач.")
    tags = models.ManyToManyField(
        Tag, blank=True, related_name="publish_configs", verbose_name="Теги",
        help_text="Порожньо = будь-які теги; інакше подія має мати ХОЧА Б ОДИН із цих тегів.")
    regions = models.ManyToManyField(
        Region, blank=True, related_name="publish_configs", verbose_name="Суб'єкти РФ",
        help_text="Порожньо = усі регіони; інакше подія має бути з ОДНОГО з обраних.")
    review_status = models.CharField(
        max_length=12, choices=Event.REVIEW_CHOICES, default=Event.REVIEW_APPROVED,
        verbose_name="Статус аудиту", help_text="Публікуємо лише події цього статусу.")
    publish_from = models.DateField(
        null=True, blank=True, verbose_name="Публікувати події від (дата)",
        help_text="Беруться лише події з event_date >= цієї дати. Порожньо = без нижньої "
                  "межі (УВАГА: перший прохід забере ВЕСЬ історичний беклог). Став дату "
                  "активації, щоб публікувати лише нові події.")

    # --- Telegram Bot API (один канал) ---
    chat_id = models.CharField(
        max_length=64, verbose_name="Chat ID каналу",
        help_text="@username каналу або числовий id (напр. -1001234567890). "
                  "Бот має бути адміном каналу.")
    bot_token = models.CharField(
        max_length=128, blank=True, verbose_name="Bot token",
        help_text="Порожньо = береться з env TELEGRAM_BOT_TOKEN.")

    # --- AI (фільтр + рерайт одним викликом) ---
    ai_model = models.CharField(
        max_length=120, blank=True, verbose_name="AI-модель",
        help_text="Порожньо = дефолтна LLM_MODEL.")
    ai_prompt = models.TextField(
        blank=True, verbose_name="AI-промпт (фільтр+рерайт)",
        help_text="Системний промпт. Порожньо = дефолт із коду (services/publish/prompts.py). "
                  "Модель має повертати JSON {publish: bool, reason, post_text}.")

    # --- throttle ---
    max_per_pass = models.PositiveIntegerField(
        default=5, verbose_name="Макс. постів за прохід",
        help_text="Скільки подій обробляти за один тік воркера (стримує вивал беклогу).")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Профіль публікації"
        verbose_name_plural = "Профілі публікації"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} → {self.chat_id}"

    def resolved_token(self):
        from django.conf import settings as dj_settings
        return (self.bot_token or "").strip() or getattr(dj_settings, "TELEGRAM_BOT_TOKEN", "")


class PublishedEvent(models.Model):
    """Стан публікації однієї події одним профілем: claim-мітка + аудит.

    Наявність рядка = подію вже взято в роботу цим профілем (unique config+event),
    тож повторний claim неможливий. status веде життєвий цикл:
      pending  — заклеймлено, ще не оброблено (locked_at — м'який лок воркера);
      skipped  — AI вирішив НЕ публікувати (ai_reason);
      published— відправлено в канал (tg_message_id, published_at);
      failed   — вичерпано спроби (error)."""

    STATUS_PENDING = "pending"
    STATUS_SKIPPED = "skipped"
    STATUS_PUBLISHED = "published"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "В обробці"),
        (STATUS_SKIPPED, "Відсіяно AI"),
        (STATUS_PUBLISHED, "Опубліковано"),
        (STATUS_FAILED, "Помилка"),
    ]

    config = models.ForeignKey(
        PublishConfig, on_delete=models.CASCADE, related_name="published",
        verbose_name="Профіль")
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="publications",
        verbose_name="Подія")
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
        verbose_name="Статус")

    ai_verdict = models.BooleanField(null=True, blank=True, verbose_name="AI: публікувати")
    ai_reason = models.TextField(blank=True, verbose_name="AI: причина")
    post_text = models.TextField(blank=True, verbose_name="Текст поста (рерайт AI)")

    tg_message_id = models.BigIntegerField(null=True, blank=True, verbose_name="TG message id")
    published_at = models.DateTimeField(null=True, blank=True, verbose_name="Опубліковано о")

    attempts = models.PositiveIntegerField(default=0, verbose_name="Спроби")
    locked_at = models.DateTimeField(null=True, blank=True, verbose_name="Заблоковано о")
    error = models.TextField(blank=True, verbose_name="Помилка")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Публікація події"
        verbose_name_plural = "Публікації подій"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["config", "event"], name="uniq_publish_config_event"),
        ]
        indexes = [models.Index(fields=["config", "status"])]

    def __str__(self):
        return f"{self.config_id}/{self.event_id}: {self.status}"


class UserProfile(models.Model):
    """Персональні налаштування юзера. Наразі — власний OpenRouter API-ключ:
    задачі аналізу, публікації та звіти цього юзера ходять під ЙОГО ключем
    (резолвиться через owner у стадіях; порожньо = глобальний ключ із env).
    Ключ задає admin в адмінці юзера (інлайн)."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="profile", verbose_name="Користувач")
    openrouter_key = models.CharField(
        max_length=200, blank=True, verbose_name="OpenRouter API-ключ",
        help_text="Персональний ключ для задач/публікацій/звітів цього юзера. "
                  "Порожньо = глобальний ключ із env (OPENROUTER_API_KEY).")

    class Meta:
        verbose_name = "Профіль користувача (LLM-ключ)"
        verbose_name_plural = "Профілі користувачів (LLM-ключі)"

    def __str__(self):
        return f"{self.user}"
