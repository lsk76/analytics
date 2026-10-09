"""Пакетні операції над спільним довідником каналів, без мережевих запитів."""
from . import common, fmt
from .batches import _batch, _write_batch
from .monitoring import _channel_card
from .registry import SCOPE_CREATE, TOOLS, ToolError, tool


@tool("channels_add_batch", group="monitoring", mutates=True, scope=SCOPE_CREATE, params={
    "items": 'JSON-масив 1–100 обʼєктів із параметрами channel_add. Наприклад: '
             '[{"url":"@alpha","title":"Альфа","topics":"новини"},'
             '{"url":"https://t.me/beta","region":"Дагестан"}]. '
             'Для кожного потрібен url; решта полів необовʼязкові.',
})
def channels_add_batch(items: str):
    """Додати канали/чати/сайти в довідник батчем (до 100 за виклик).

    items — рядок із JSON-масивом параметрів channel_add: url, title, region,
    topics (рядок через кому), chat_type, language, subscribers, audience_note.
    Повторний виклик не створює дубль: дописує порожні поля й теми наявного.
    Кожен запис обробляється окремо: помилка відкочує його зміни, решта
    виконується. Відповідь містить номер, id і результат кожного запису.
    Це лише довідник; підписки й whitelist додають source_add / chat_add.
    """
    return _write_batch(items, "channel_add")


@tool("channels_update_batch", group="monitoring", mutates=True, params={
    "items": 'JSON-масив 1–100 обʼєктів із параметрами channel_update. Наприклад: '
             '[{"ref":"#123","add_topics":"політика","subscribers":10000},'
             '{"ref":"@beta","title":"Бета","discusses_problems":false}]. '
             'Для кожного потрібен ref; кожен канал може мати власні правки.',
})
def channels_update_batch(items: str):
    """Редагувати канали довідника батчем, із власними правками для кожного.

    items — рядок із JSON-масивом параметрів channel_update: ref, add_topics,
    remove_topics, title, region, settlement, chat_type, focus,
    discusses_problems (JSON boolean), subscribers, audience_note.
    ref — id (#123), @username, посилання або однозначна частина назви.
    Пропущені поля й null не змінюються; '-' очищає region/settlement/focus;
    subscribers=0 обнуляє аудиторію. До 100 записів за виклик, у порядку вводу.
    Помилка відкочує лише відповідний запис; результат показано для кожного.
    """
    return _write_batch(items, "channel_update")


@tool("channels_get_batch", group="monitoring", params={
    "refs": 'Канали довідника: id (#123), @username, посилання або однозначна '
            'частина назви. Рядок через кому ("#123, @beta") або рядок із '
            'JSON-масивом (["#123","@beta"]). Від 1 до 100 посилань.',
})
def channels_get_batch(refs: str):
    """Отримати повні картки кількох каналів довідника за один виклик.

    refs — рядок через кому або рядок із JSON-масивом id/@username/посилань/
    однозначних назв. До 100 каналів, у порядку вводу. Для кожного повертає
    id, поля картки, фокус, мову й ознаку обговорення проблем; відсутній або
    неоднозначний канал має окрему помилку. Пошук за частиною username/назви
    з кількома збігами — channels_find. Читає лише нашу БД.
    """
    data = _batch(refs, "refs", csv=True)
    results, errors = [], 0
    for index, ref in enumerate(data, 1):
        try:
            if not isinstance(ref, str) or not ref.strip():
                raise ToolError("ref: очікується непорожній рядок")
            ch = common.resolve_channel(ref)
            card = _channel_card(ch) + "\n" + fmt.kv([
                ("мова", ch.language or "—"),
                ("фокус", ch.directory_focus or "—"),
                ("обговорює проблеми", fmt.flag(ch.discusses_problems)),
                ("деталі", f"/admin/analysis/channel/{ch.id}/change/"),
            ])
            results.append(fmt.section(f"Запис {index}: #{ch.id} · {ref}", card))
        except ToolError as e:
            errors += 1
            results.append(fmt.section(f"Запис {index}: ПОМИЛКА", str(e)))
    return fmt.section(
        f"Довідник: знайдено {len(data) - errors}, помилок {errors}, усього {len(data)}",
        "\n\n".join(results))
