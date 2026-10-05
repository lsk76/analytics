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
import zipfile
from pathlib import PurePosixPath

from django.core.files.base import ContentFile

from telethon.sessions import SQLiteSession, StringSession
from telethon.sessions.sqlite import EXTENSION as _SQLITE_EXT
from django.db import IntegrityError, transaction

from ..models import AccountTag, TelegramAccount
from .proxy_assignment import random_working_proxy

# lang_pack у tdata JSON — не ISO-код мови, а назва пака Telegram Desktop;
# "tdesktop" = базовий (англійський) пак.
_LANG_PACK_TO_CODE = {"tdesktop": "en"}

_ARCHIVE_MAX_FILES = 1000
_ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
_ARCHIVE_MAX_FILE_BYTES = 8 * 1024 * 1024


def _archive_uploads(upload, remaining_bytes):
    """Читаємо ZIP без розпакування на диск; імена зіставляються без папок."""
    files = []
    total = 0
    with zipfile.ZipFile(upload) as archive:
        members = archive.infolist()
        if len(members) > _ARCHIVE_MAX_FILES:
            raise ValueError("У ZIP забагато файлів (максимум 1000)")
        for member in members:
            path = PurePosixPath(member.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("ZIP містить небезпечний шлях")
            if member.is_dir() or "__MACOSX" in path.parts or path.name.startswith("._"):
                continue
            if path.suffix.lower() not in (".json", ".session"):
                continue
            if member.flag_bits & 1:
                raise ValueError("ZIP із паролем не підтримується")
            if member.file_size > _ARCHIVE_MAX_FILE_BYTES:
                raise ValueError("Файл у ZIP перевищує 8 МБ")
            total += member.file_size
            if total > remaining_bytes:
                raise ValueError("Розмір файлів із ZIP перевищує загальний ліміт 64 МБ")
            with archive.open(member) as source:
                data = source.read(_ARCHIVE_MAX_FILE_BYTES + 1)
            if len(data) != member.file_size or len(data) > _ARCHIVE_MAX_FILE_BYTES:
                raise ValueError("Некоректний розмір файлу у ZIP")
            files.append(ContentFile(data, name=path.name))
    if not files:
        raise ValueError("У ZIP немає .json або .session файлів")
    return files, total


def convert_sqlite_to_string_session(session_file_path: str) -> str:
    """session_file_path — реальний шлях до .session файлу на диску."""
    path = session_file_path
    if not path.endswith(_SQLITE_EXT):
        path += _SQLITE_EXT
        os.rename(session_file_path, path)
    sqlite_sess = SQLiteSession(path)
    try:
        if not sqlite_sess.auth_key:
            raise ValueError("У сесії немає ключа авторизації")
        string_sess = StringSession()
        string_sess.set_dc(sqlite_sess.dc_id, sqlite_sess.server_address, sqlite_sess.port)
        string_sess.auth_key = sqlite_sess.auth_key
        return string_sess.save()
    finally:
        sqlite_sess.close()


def import_tdata_account(meta: dict, session_file_path: str, owner,
                         tag_names: list | None = None) -> TelegramAccount:
    """meta — розпарсений <phone>.json. Кидає ValueError/IntegrityError на невалідні дані.

    owner — власник акаунта (None = спільний, бачать усі)."""
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
        proxy=random_working_proxy(),
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
    if not isinstance(meta, dict):
        raise ValueError("JSON має бути обʼєктом із полем phone")
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


def import_tdata_accounts_from_uploads(files, owner, tag_names=None):
    """Зіставити завантаження за точним іменем без розширення; кожна пара незалежна.

    Звіт не містить вмісту файлів або тексту винятків, які можуть містити секрети.
    Дублікати файлів неоднозначні: таку пару не імпортуємо.
    """
    pairs = {}
    results = []
    expanded = []
    archive_bytes = 0
    for upload in files:
        if os.path.splitext(upload.name)[1].lower() != ".zip":
            expanded.append(upload)
            continue
        try:
            archive_files, size = _archive_uploads(upload, _ARCHIVE_MAX_BYTES - archive_bytes)
        except ValueError as exc:
            results.append({"name": upload.name, "ok": False, "detail": str(exc)})
        except Exception:
            results.append({"name": upload.name, "ok": False,
                            "detail": "Не вдалося прочитати ZIP: архів пошкоджений або формат не підтримується"})
        else:
            expanded.extend(archive_files)
            archive_bytes += size
    for upload in expanded:
        stem, extension = os.path.splitext(upload.name)
        extension = extension.lower()
        if extension not in (".json", ".session"):
            results.append({"name": upload.name, "ok": False,
                            "detail": "Непідтримуваний формат: потрібні .json, .session або .zip"})
            continue
        pairs.setdefault(stem, {}).setdefault(extension, []).append(upload)

    for stem, pair in sorted(pairs.items()):
        result = {"name": stem, "ok": False}
        results.append(result)
        if any(len(uploads) > 1 for uploads in pair.values()):
            result["detail"] = "Повторюється імʼя файлу; залиште одну JSON + session пару"
            continue
        missing = [ext for ext in (".json", ".session") if ext not in pair]
        if missing:
            result["detail"] = "Немає парного файлу: " + stem + missing[0]
            continue
        try:
            with transaction.atomic():
                account = import_tdata_account_from_uploads(
                    pair[".json"][0], pair[".session"][0], owner, tag_names)
        except IntegrityError:
            result["detail"] = "Акаунт із цим номером уже є в базі або дані порушують обмеження бази"
        except Exception:
            result["detail"] = "Не вдалося імпортувати: перевірте JSON (поле phone) і сесію з ключем авторизації"
        else:
            detail = (f"Імпортовано; призначено проксі #{account.proxy_id}"
                      if account.proxy_id else
                      "Імпортовано без проксі: немає активних робочих проксі")
            result.update(ok=True, account=account, detail=detail)
    return results
