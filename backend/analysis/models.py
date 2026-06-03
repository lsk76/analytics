"""
Конфігурований фреймворк аналізу Telegram → події.

Пайплайн:  AnalysisTask (конфігурація)
  collect (запит TeleZip) -> Post
  enrich (Telethon: надійна дата + метадані Channel)
  classify (LLM за схемою задачі) -> сирі поля
  normalize (вільний текст -> канонічне через таблиці аліасів: Nationality, ConflictType)
  dedup (попарно LLM у вікні) -> Event (M2M сторони, 1 подія <- N постів)

«Етнічні сутички 2025» — це ОДИН рядок AnalysisTask; ніщо тут не захардкоджено під неї.
"""
from django.db import models


# ---------------------------------------------------------------------------
# Конфігурація задачі — параметризує весь пайплайн
# ---------------------------------------------------------------------------

class AnalysisTask(models.Model):
    name = models.CharField(max_length=200, verbose_name="Назва")
    slug = models.SlugField(unique=True, verbose_name="Ідентифікатор (slug)")
    description = models.TextField(blank=True, verbose_name="Опис")

    # Збір (TeleZip)
    telezip_query = models.TextField(
        verbose_name="Пошуковий запит TeleZip",
        help_text="Запит у синтаксисі TeleZip (text=...)",
    )
    date_from = models.DateField(verbose_name="Дата від")
    date_to = models.DateField(verbose_name="Дата до")
    languages = models.JSONField(
        default=list, blank=True, verbose_name="Мови",
        help_text='Список кодів мов, напр. ["ru"]',
    )
    search_posts = models.BooleanField(
        default=True, verbose_name="Шукати по постах",
        help_text="Включати пости каналів",
    )
    search_comments = models.BooleanField(
        default=False, verbose_name="Шукати по коментарях",
        help_text="Включати коментарі/повідомлення чатів",
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
    relevance_field = models.CharField(
        max_length=50, default="is_relevant", verbose_name="Поле релевантності",
        help_text="Булеве поле у відповіді LLM, що пропускає пост у події.",
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

    class Meta:
        verbose_name = "Канал"
        verbose_name_plural = "Канали"
        constraints = [
            models.UniqueConstraint(fields=["tg_id"], name="uniq_channel_tgid",
                                    condition=models.Q(tg_id__isnull=False)),
        ]

    def __str__(self):
        return self.username or self.title or f"channel#{self.tg_id}"


# ---------------------------------------------------------------------------
# Керовані довідники (відкриті + авто-мапінг аліасів -> без дублів за сенсом)
# ---------------------------------------------------------------------------

class Tag(models.Model):
    """
    Канонічна сторона/аспект конфлікту з категорією.
    nationality — закритий сід-список; решта категорій канонізуються LLM.
    """
    CATEGORY_CHOICES = [
        ("nationality", "Національність"),
        ("status", "Статус (мігрант/місцеві)"),
        ("religion", "Релігія"),
        ("role", "Роль/професія/вік"),
        ("group", "Організація/спільнота"),
        ("other", "Інше"),
    ]
    name = models.CharField(max_length=80, verbose_name="Назва (канонічна)")
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES,
                                default="other", db_index=True, verbose_name="Категорія")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Тег"
        verbose_name_plural = "Теги"
        ordering = ["category", "name"]
        constraints = [
            models.UniqueConstraint(fields=["name", "category"], name="uniq_tag_name_category"),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"


class TagAlias(models.Model):
    """Зіставляє будь-який варіант вільного тексту -> канонічний Tag (ключ у нижньому регістрі)."""
    raw = models.CharField(max_length=120, unique=True, verbose_name="Варіант (аліас)")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="aliases", verbose_name="Тег")

    class Meta:
        verbose_name = "Аліас тега"
        verbose_name_plural = "Аліаси тегів"

    def __str__(self):
        return f"{self.raw} -> {self.tag_id}"


class ConflictType(models.Model):
    name = models.CharField(max_length=80, unique=True, verbose_name="Назва (канонічна)")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Тип конфлікту"
        verbose_name_plural = "Типи конфліктів"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ConflictTypeAlias(models.Model):
    raw = models.CharField(max_length=120, unique=True, verbose_name="Варіант (аліас)")
    conflict_type = models.ForeignKey(
        ConflictType, on_delete=models.CASCADE, related_name="aliases",
        verbose_name="Тип конфлікту",
    )

    class Meta:
        verbose_name = "Аліас типу конфлікту"
        verbose_name_plural = "Аліаси типів конфліктів"

    def __str__(self):
        return f"{self.raw} -> {self.conflict_type_id}"


class Region(models.Model):
    """Канонічний суб'єкт РФ (область/республіка/край/місто федерального значення/АО)."""
    KIND_CHOICES = [
        ("республіка", "Республіка"), ("область", "Область"), ("край", "Край"),
        ("місто", "Місто федерального значення"), ("ао", "Автономний округ/область"),
    ]
    name = models.CharField(max_length=120, unique=True, verbose_name="Назва (канонічна)")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, blank=True, verbose_name="Тип")
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
    """Один запуск задачі за період — зберігає параметри, статус і агреговані результати."""

    STATUS_CHOICES = [
        ("pending", "Очікує"),
        ("collecting", "Збір"),
        ("enriching", "Збагачення"),
        ("classifying", "Класифікація"),
        ("deduplicating", "Дедуплікація"),
        ("completed", "Завершено"),
        ("failed", "Помилка"),
    ]

    task = models.ForeignKey(AnalysisTask, on_delete=models.CASCADE, related_name="runs", verbose_name="Задача")
    title = models.CharField(max_length=200, blank=True, verbose_name="Назва дослідження")
    date_from = models.DateField(verbose_name="Період від")
    date_to = models.DateField(verbose_name="Період до")

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
        verbose_name = "Дослідження (запуск)"
        verbose_name_plural = "Дослідження (запуски)"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or f"{self.task.slug} {self.date_from}…{self.date_to}"


# ---------------------------------------------------------------------------
# Пости та події
# ---------------------------------------------------------------------------

class Post(models.Model):
    task = models.ForeignKey(AnalysisTask, on_delete=models.CASCADE, related_name="posts", verbose_name="Задача")
    run = models.ForeignKey(ResearchRun, on_delete=models.SET_NULL, null=True, blank=True,
                            related_name="posts", verbose_name="Запуск")
    url = models.URLField(max_length=500, verbose_name="Посилання")
    channel = models.ForeignKey(
        Channel, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="posts", verbose_name="Канал",
    )
    channel_name = models.CharField(max_length=128, blank=True, verbose_name="Назва каналу")
    posted_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Дата публікації",
        help_text="Надійна дата (Telethon/TeleZip, UTC)",
    )
    telezip_date = models.DateTimeField(null=True, blank=True, verbose_name="Дата TeleZip")
    text = models.TextField(blank=True, verbose_name="Текст")
    content_hash = models.CharField(max_length=80, blank=True, db_index=True, verbose_name="Хеш контенту")
    telezip_mid = models.BigIntegerField(null=True, blank=True, verbose_name="TeleZip MID")

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
        indexes = [models.Index(fields=["task", "is_classified"])]

    def __str__(self):
        return self.url


class Event(models.Model):
    """Дедуплікований реальний інцидент (1 подія <- N постів)."""
    task = models.ForeignKey(AnalysisTask, on_delete=models.CASCADE, related_name="events", verbose_name="Задача")
    run = models.ForeignKey(ResearchRun, on_delete=models.CASCADE, null=True, blank=True,
                            related_name="events", verbose_name="Запуск")
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
    conflict_type = models.ForeignKey(
        ConflictType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="events", verbose_name="Тип конфлікту",
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="events", verbose_name="Сторони/теги")
    summary = models.TextField(blank=True, verbose_name="Опис")
    post_count = models.PositiveIntegerField(default=0, verbose_name="Кількість постів")
    reach = models.BigIntegerField(
        default=0, verbose_name="Охоплення",
        help_text="Сумарна к-сть підписників унікальних каналів події",
    )
    is_corroborated = models.BooleanField(
        default=False, verbose_name="Підтверджено",
        help_text="Підтверджено ≥2 каналами",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Подія"
        verbose_name_plural = "Події"
        ordering = ["event_date"]
        indexes = [models.Index(fields=["task", "event_date"])]

    def __str__(self):
        return f"{self.event_date} {self.region}: {self.summary[:60]}"
