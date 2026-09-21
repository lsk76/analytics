"""Запобіжник: Telethon живе ТІЛЬКИ в accounts/gateway/ (docs/tg-gateway-plan.md).

Будь-який інший модуль, що імпортує telethon або будує TelegramClient, — це
повернення до «кожен споживач сам ходить у Telegram», з якого й росли вбиті
сесії. Виняток лише для явно перелічених legacy-скриптів."""
import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[2]
ALLOWED_DIRS = ("accounts/gateway",)
LEGACY = {
    # ad-hoc дослідницький збір по датах: окремий примітив, у gateway не переносили
    "analysis/management/commands/monitor_sample_collect.py",
    # офлайн-конвертація tdata → StringSession: без мережі, в Telegram не заходить
    "accounts/services/tdata_import.py",
}
PATTERN = re.compile(r"^\s*(from\s+telethon|import\s+telethon)|TelegramClient\(", re.M)


def test_no_telethon_outside_gateway():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(ALLOWED_DIRS) or rel in LEGACY or "/tests/" in f"/{rel}" \
                or rel.startswith("_dir/") or "/migrations/" in f"/{rel}":
            continue
        if PATTERN.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(rel)
    assert offenders == [], f"Telethon поза gateway: {offenders}"
