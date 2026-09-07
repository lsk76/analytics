"""Транслітерація кирилиці в латиницю для username бота (@BotFather вимагає
латиницю+цифри, закінчення на "bot"). Той самий алфавіт продубльовано в JS
у шаблоні create_bot.html для миттєвої підказки — тут лише серверна
нормалізація/фолбек, якщо оператор лишив поле порожнім."""
import re

_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
    "ъ": "", "ы": "y", "э": "e", "ю": "iu", "я": "ia",
}


def transliterate(text: str) -> str:
    return "".join(_TRANSLIT_MAP.get(ch, ch) for ch in text.lower())


def slugify_bot_username(name: str) -> str:
    """Кандидат на username бота: латиниця+цифри, закінчення "bot", 5-32 символи."""
    base = re.sub(r"[^a-z0-9]+", "", transliterate(name))
    if not base:
        base = "my"
    if not base[0].isalpha():
        base = "a" + base
    while len(base) < 2:  # мінімум 2 символи перед "bot" => разом >=5
        base += "x"
    if not base.endswith("bot"):
        base += "bot"
    return base[:32]


def normalize_bot_username(raw: str) -> str:
    """Оператор ввів свій варіант — лише прибрати недопустимі символи й дописати "bot"."""
    base = re.sub(r"[^a-zA-Z0-9]+", "", raw)
    if not base:
        return slugify_bot_username(raw)
    if not base.lower().endswith("bot"):
        base += "bot"
    if not base[0].isalpha():
        base = "a" + base
    return base[:32]
