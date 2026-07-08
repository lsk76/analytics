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
from django.db import models
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

    # --- вибір конвеєра: які stage-воркери обробляють пости задачі ---
    PIPELINE_EVENTS = "events"
    PIPELINE_MONITOR = "monitor"
    PIPELINE_RESEARCH = "research"
    PIPELINE_CHOICES = [
        (PIPELINE_EVENTS, "Події (enrich→precluster→classify→dedup)"),
        (PIPELINE_MONITOR, "Моніторинг думок (filter→prescreen→tag)"),
        (PIPELINE_RESEARCH, "Тематичне дослідження (канали→рубрики→агенти→дедуп)"),
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
    STAGE_CHOICES = [
        (STAGE_COLLECTED, "Зібрано"),
        (STAGE_ENRICHED, "Збагачено"),
        (STAGE_PRECLUSTERED, "Прекластеризовано"),
        (STAGE_CLASSIFIED, "Класифіковано"),
        (STAGE_DEDUPED, "Дедупльовано"),
        (STAGE_MON_COLLECTED, "Монітор: зібрано"),
        (STAGE_MON_FILTERED, "Монітор: відфільтровано"),
        (STAGE_MON_PRESCREENED, "Монітор: прескрін+"),
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
    priority = models.PositiveSmallIntegerField(
        default=100, verbose_name="Пріоритет",
        help_text="Менше = вище у списку. Для сортування при показі.",
    )
    notes = models.TextField(blank=True, verbose_name="Нотатки")
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
