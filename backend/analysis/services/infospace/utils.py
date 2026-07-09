"""Спільні утиліти infospace-конвеєра."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Клік-трекери поза utm_* (utm_* зрізаються за префіксом)
_DROP_PARAMS = {"yclid", "fbclid", "gclid", "ysclid", "_openstat"}


def canonical_url(url: str) -> str:
    """Канонізує посилання на матеріал, щоб той самий текст із RSS і зі
    скрапінгу сайту схлопнувся в один Post (unique (task, url)).

    Правила: хост у нижній регістр; #fragment геть; параметри utm_* і
    відомі клік-трекери геть; решта параметрів — як були (порядок зберігається).
    Не-URL ідентифікатори (@username тощо) повертаються як є.
    """
    url = (url or "").strip()
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        # биті href зі скрапінгу сайту (напр. «Invalid IPv6 URL») не мають
        # валити стадію збору — повертаємо як є, далі відсіє unique/скрін
        return url
    if not parts.scheme or not parts.netloc:
        return url  # tg-ідентифікатори і відносні шляхи не чіпаємо
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _DROP_PARAMS
    ]
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path,
        urlencode(query),
        "",  # fragment
    ))
