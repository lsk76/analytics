"""Пакетне керування джерелами зі стандартними правами й видимістю."""
from analysis.models import Source, SourceSubscription

from . import common, fmt
from .batches import _batch, _write_batch
from .registry import SCOPE_CREATE, ToolError, tool


def _resolve_visible(ref):
    # Видимість задає access.py, включно з вимкненими власними підписками.
    qs = common.scope(Source.objects.select_related("channel", "channel__region_subject"))
    if ref.lstrip("#").isdigit():
        qs = qs.filter(pk=int(ref.lstrip("#")))
    else:
        from django.db.models import Q
        qs = qs.filter(Q(channel__url__icontains=ref) | Q(channel__title__icontains=ref))
    return common._pick(qs.order_by("id"), ref, "джерело",
                        lambda s: f"#{s.id} {s.name} ({s.url})")


def _prepare_update(payload, size):
    if size > 1 and payload.get("reset_cursor") and not payload.get("confirm"):
        raise ToolError("reset_cursor у батчі перечитає джерело з нуля — потрібне confirm=true")
    # Одна правка = одне джерело. Передаємо id, щоб URL із комою не став
    # вкладеним CSV-батчем у source_update.
    source = _resolve_visible(payload["ref"].strip())
    return {**payload, "ref": f"#{source.id}"}


def _prepare_add(payload, size):
    # Повторний source_add може міняти інтервал/акаунт уже наявного
    # спільного джерела: для цього діють ті самі обмеження, що й для update.
    if payload.get("poll_interval_sec") or payload.get("account"):
        from analysis.services.directory import normalize_url
        from .monitoring import _own_source_or_die
        _, url = normalize_url(payload["url"], kind_hint=payload.get("kind", ""))
        existing = Source.objects.filter(channel__url=url).first() if url else None
        if existing:
            _own_source_or_die(existing)
    return payload


@tool("sources_add_batch", group="monitoring", mutates=True, scope=SCOPE_CREATE, params={
    "items": 'Рядок із JSON-масивом 1–100 обʼєктів із параметрами source_add: '
             'url (обовʼязковий), kind, task, name, region, language, '
             'poll_interval_sec, account. Приклад: '
             '[{"url":"https://example.org/feed","kind":"rss","task":"news"}].',
})
def sources_add_batch(items: str):
    """Додати джерела інформпростору батчем до 100 записів.

    items — рядок із JSON-масивом параметрів source_add. Кожен запис може
    мати власну задачу, тип, назву, регіон, інтервал та акаунт. Повторне
    посилання не дублює джерело. task — лише власна infospace-задача;
    без її активної підписки воркер не опитує джерело. Виклик не збирає дані.
    Помилка відкочує весь відповідний запис (канал, джерело, підписку);
    решта виконується. Результати й id джерел повертаються у порядку вводу.
    """
    return _write_batch(items, "source_add", "Джерела", prepare=_prepare_add)


@tool("sources_update_batch", group="monitoring", mutates=True, params={
    "items": 'Рядок із JSON-масивом 1–100 обʼєктів із параметрами source_update: '
             'ref (одне джерело: id/URL/однозначна назва), is_active, '
             'poll_interval_sec, poll_now, reset_cursor, account, confirm. '
             'Приклад: [{"ref":"#12","is_active":false},'
             '{"ref":"#13","poll_interval_sec":900}].',
})
def sources_update_batch(items: str):
    """Редагувати до 100 джерел із власним набором правок для кожного.

    items — рядок із JSON-масивом параметрів source_update. ref позначає
    ОДНЕ джерело (id/#id, URL/частина URL, однозначна назва). Пропущені поля
    й null не змінюються; account='-' відвʼязує акаунт. Інтервал мінімум 60с.
    reset_cursor=true в батчі з кількома записами потребує confirm=true
    у відповідному записі: джерело буде перечитане з нуля.
    Чужі джерела недоступні, стандартні обмеження source_update збережені.
    Помилка відкочує лише її запис; решта виконується у порядку вводу.
    """
    return _write_batch(items, "source_update", "Джерела", prepare=_prepare_update)


@tool("sources_get_batch", group="monitoring", params={
    "refs": 'Від 1 до 100 джерел: id/#id, URL/частина URL або однозначна назва. '
            'Рядок через кому ("#12, #13") або рядок із JSON-масивом '
            '(["#12","https://example.org/feed"]).',
})
def sources_get_batch(refs: str):
    """Отримати картки до 100 видимих джерел за один виклик.

    refs — рядок через кому або рядок із JSON-масивом id/URL/назв.
    Повертає id джерела й каналу, тип, назву, регіон, мову, активність,
    інтервал і час полінгу, health/якість та лише видимі підписки.
    Вимкнена власна підписка не приховує джерело. Чуже, відсутнє або
    неоднозначне джерело має окрему помилку; решта карток повертається
    у порядку вводу. Читає лише БД, без зовнішніх запитів.
    """
    data = _batch(refs, "refs", csv=True)
    results, errors = [], 0
    for index, ref in enumerate(data, 1):
        try:
            if not isinstance(ref, str) or not ref.strip():
                raise ToolError("ref: очікується непорожній рядок")
            src = _resolve_visible(ref.strip())
            subscriptions = common.scope(SourceSubscription.objects.filter(source=src)) \
                .select_related("task").order_by("id")
            account = "—"
            if src.tg_account_id:
                from accounts.models import TelegramAccount
                account = (f"#{src.tg_account_id}" if common.scope(
                    TelegramAccount.objects.filter(pk=src.tg_account_id)).exists() else "недоступний")
            card = fmt.kv([
                ("канал", f"#{src.channel_id}"), ("назва", src.name),
                ("посилання", src.url), ("тип", src.kind),
                ("регіон", src.region_subject.name if src.region_subject_id else "—"),
                ("мова", src.language or "—"), ("активне", fmt.flag(src.is_active)),
                ("інтервал, с", src.poll_interval_sec), ("акаунт", account),
                ("наступний полінг", str(src.next_poll_at or "—")),
                ("останній успіх", str(src.last_ok_at or "—")),
                ("збоїв поспіль", str(src.consecutive_failures)),
                ("остання помилка", src.last_error or "—"),
                ("якість", fmt.flag(src.quality_ok)),
                ("нотатка якості", src.quality_note or "—"),
                ("скрапер", src.scraper_key or "авто"),
                ("підписки", ", ".join(
                    f"#{s.task_id} {s.task.slug} ({'активна' if s.is_active else 'вимкнена'})"
                    for s in subscriptions) or "—"),
                ("деталі", f"/admin/analysis/source/{src.id}/change/"),
            ])
            results.append(fmt.section(f"Запис {index}: #{src.id} · {ref}", card))
        except ToolError as e:
            errors += 1
            results.append(fmt.section(f"Запис {index}: ПОМИЛКА", str(e)))
    return fmt.section(f"Джерела: знайдено {len(data) - errors}, помилок {errors}, усього {len(data)}",
                       "\n\n".join(results))
