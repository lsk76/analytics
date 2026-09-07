"""spam_status_check — taskless-стадія для run_worker.py: періодична перевірка
обмежень акаунтів через @SpamBot.

Обмеження Telegram на резолв юзернеймів/надсилання (не мертва проксі) —
головна причина, чому «голі»/непрогріті акаунти валять тестовий прогін
бота (UsernameNotOccupiedError навіть на @BotFather). /start до @SpamBot —
офіційний спосіб дізнатись цей стан, тож перевіряємо його регулярно й
самостійно, щоб оператор бачив статус у таблиці акаунтів без ручного кліку.
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone as djtz

from ..models import TelegramAccount
from .telegram_client import TelegramUserClient

CHECK_INTERVAL = timedelta(hours=24)


def _claim_account():
    now = djtz.now()
    cutoff = now - CHECK_INTERVAL
    with transaction.atomic():
        acc = (TelegramAccount.objects
               .filter(is_active=True, is_authenticated=True)
               .filter(Q(spam_status_checked_at__isnull=True)
                      | Q(spam_status_checked_at__lt=cutoff))
               .select_for_update(skip_locked=True)
               .order_by(F("spam_status_checked_at").asc(nulls_first=True))
               .first())
        if acc:
            # проставляємо одразу — і як "захоплено" (щоб інший цикл не забрав
            # повторно), і як фактичний час перевірки (нижче лише уточнюється)
            acc.spam_status_checked_at = now
            acc.save(update_fields=["spam_status_checked_at"])
    return acc


def spam_status_check_once() -> bool:
    acc = _claim_account()
    if not acc:
        return False

    res = TelegramUserClient.check_spam_status_sync(acc)
    acc.spam_status = res.get("status", "unknown")
    acc.spam_status_detail = (res.get("detail") or "")[:300]
    acc.spam_status_checked_at = djtz.now()
    acc.save(update_fields=["spam_status", "spam_status_detail", "spam_status_checked_at"])
    return True
