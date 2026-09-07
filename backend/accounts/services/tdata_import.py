"""Імпорт акаунта з пари файлів tdata-експорту: <phone>.json (метадані) +
<phone>.session (Telethon SQLiteSession — та сама схема, що дає стандартний
експорт tdata-конвертерів: update_state/sent_files/entities/sessions/version).

Перетворюємо SQLiteSession -> StringSession (формат, який зберігає проєкт у
TelegramAccount.session_string), переносимо device-відбиток (щоб Telegram не
побачив «стрибок» пристрою в активних сесіях) і, за наявності, 2FA-пароль.
"""
import json
import os
import tempfile

from telethon.sessions import SQLiteSession, StringSession
from telethon.sessions.sqlite import EXTENSION as _SQLITE_EXT

from ..models import AccountTag, TelegramAccount

# lang_pack у tdata JSON — не ISO-код мови, а назва пака Telegram Desktop;
# "tdesktop" = базовий (англійський) пак.
_LANG_PACK_TO_CODE = {"tdesktop": "en"}


def convert_sqlite_to_string_session(session_file_path: str) -> str:
    """session_file_path — реальний шлях до .session файлу на диску."""
    path = session_file_path
    if not path.endswith(_SQLITE_EXT):
        path += _SQLITE_EXT
        os.rename(session_file_path, path)
    sqlite_sess = SQLiteSession(path)
    string_sess = StringSession()
    string_sess.set_dc(sqlite_sess.dc_id, sqlite_sess.server_address, sqlite_sess.port)
    string_sess.auth_key = sqlite_sess.auth_key
    return string_sess.save()


def import_tdata_account(meta: dict, session_file_path: str, owner,
                         tag_names: list | None = None) -> TelegramAccount:
    """meta — розпарсений <phone>.json. Кидає ValueError/IntegrityError на невалідні дані."""
    session_string = convert_sqlite_to_string_session(session_file_path)

    phone = str(meta.get("phone") or "").strip()
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"
    if not phone:
        raise ValueError("У JSON немає поля 'phone'")

    name = " ".join(filter(None, [meta.get("first_name"), meta.get("last_name")])).strip()
    lang_pack = (meta.get("lang_pack") or "").strip()
    system_lang_pack = (meta.get("system_lang_pack") or "").strip()

    account = TelegramAccount.objects.create(
        user=owner,
        name=name or phone,
        phone_number=phone,
        api_id=str(meta.get("app_id") or "") or None,
        api_hash=meta.get("app_hash") or None,
        session_string=session_string,
        is_authenticated=True,
        two_fa_password=meta.get("twoFA") or "",
        device_model=(meta.get("device") or "")[:100],
        system_version=(meta.get("sdk") or "")[:64],
        app_version=(meta.get("app_version") or "")[:64],
        lang_code=_LANG_PACK_TO_CODE.get(lang_pack, lang_pack[:8]),
        system_lang_code=(system_lang_pack.split("-")[0][:8] if system_lang_pack else ""),
    )
    for tname in (tag_names or []):
        tname = tname.strip()
        if not tname:
            continue
        tag, _ = AccountTag.objects.get_or_create(name=tname)
        account.tags.add(tag)
    return account


def import_tdata_account_from_uploads(json_file, session_file, owner,
                                      tag_names: list | None = None) -> TelegramAccount:
    """json_file/session_file — Django UploadedFile (з request.FILES)."""
    meta = json.loads(json_file.read())
    with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as tmp:
        for chunk in session_file.chunks():
            tmp.write(chunk)
        tmp_path = tmp.name
    try:
        return import_tdata_account(meta, tmp_path, owner, tag_names)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
