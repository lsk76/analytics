import json
import io
import zipfile

import pytest
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from accounts.models import TelegramAccount
from accounts.services.tdata_import import import_tdata_accounts_from_uploads

pytestmark = pytest.mark.django_db


@pytest.fixture
def session_bytes(tmp_path):
    path = tmp_path / "fixture.session"
    session = SQLiteSession(str(path))
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    session.save()
    session.close()
    return path.read_bytes()


def pair(name, session_bytes, phone=None):
    return [
        SimpleUploadedFile(name + ".json", json.dumps({
            "phone": phone or name, "first_name": "Test", "app_id": 123,
            "app_hash": "0123456789abcdef0123456789abcdef",
        }).encode()),
        SimpleUploadedFile(name + ".session", session_bytes),
    ]


def test_browser_imports_40_files_in_any_order(client, django_user_model, session_bytes):
    admin = django_user_model.objects.create_superuser("import-admin", password="x")
    client.force_login(admin)
    files = [file for i in range(20) for file in pair(str(70000000000 + i), session_bytes)]
    response = client.post(reverse("admin:accounts_telegramaccount_import_tdata"),
                           {"files": files[::-1], "tags": "batch, test"})
    assert response.status_code == 200
    assert len(response.context["results"]) == 20
    assert all(row["ok"] for row in response.context["results"])
    assert TelegramAccount.objects.count() == 20
    for account in TelegramAccount.objects.all():
        assert account.user_id is None
        assert account.is_authenticated
        assert set(account.tags.values_list("name", flat=True)) == {"batch", "test"}
        assert StringSession(account.session_string).auth_key.key == bytes(range(256))
    assert b'name="files"' in response.content
    assert b'multiple required' in response.content


def test_bad_pairs_do_not_stop_good_accounts(session_bytes):
    TelegramAccount.objects.create(phone_number="+existing")
    files = (
        pair("good", session_bytes)
        + pair("duplicate-phone", session_bytes, "existing")
        + pair("invalid-session", b"not a SQLite session")
        + [SimpleUploadedFile("missing.json", b'{}')]
        + pair("ambiguous", session_bytes)
        + [SimpleUploadedFile("ambiguous.json", b'{}')]
        + [SimpleUploadedFile("wrong.txt", b"ignored")]
        + [SimpleUploadedFile("bad-json.json", b'{"secret":"sensitive-value", broken')]
        + [SimpleUploadedFile("bad-json.session", session_bytes)]
    )
    results = import_tdata_accounts_from_uploads(files, None)
    assert sum(row["ok"] for row in results) == 1
    assert len(results) == 7
    assert set(TelegramAccount.objects.values_list("phone_number", flat=True)) == {
        "+good", "+existing"}
    assert "sensitive-value" not in str(results)


def test_each_pair_rolls_back_on_tag_failure(monkeypatch, session_bytes):
    from accounts.services import tdata_import

    def fail_tags(*args, **kwargs):
        raise RuntimeError("secret error contents")

    monkeypatch.setattr(tdata_import.AccountTag.objects, "get_or_create", fail_tags)
    results = import_tdata_accounts_from_uploads(pair("rollback", session_bytes), None, ["tag"])
    assert not results[0]["ok"]
    assert not TelegramAccount.objects.exists()
    assert "secret error contents" not in str(results)


def test_staff_imports_owned_accounts(client, django_user_model, session_bytes):
    user = django_user_model.objects.create_user("import-user", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="add_telegramaccount"))
    client.force_login(user)
    response = client.post(reverse("admin:accounts_telegramaccount_import_tdata"),
                           {"files": pair("owned", session_bytes)})
    assert response.status_code == 200
    assert TelegramAccount.objects.get().user_id == user.pk


def test_import_requires_add_permission(client, django_user_model, session_bytes):
    user = django_user_model.objects.create_user("no-import", is_staff=True)
    client.force_login(user)
    url = reverse("admin:accounts_telegramaccount_import_tdata")
    assert client.get(url).status_code == 403
    assert client.post(url, {"files": pair("forbidden", session_bytes)}).status_code == 403
    assert not TelegramAccount.objects.exists()


@pytest.mark.parametrize("json_bytes", [b"[]", b"{}"])
def test_invalid_metadata_is_reported(json_bytes, session_bytes):
    results = import_tdata_accounts_from_uploads([
        SimpleUploadedFile("invalid.json", json_bytes),
        SimpleUploadedFile("invalid.session", session_bytes),
    ], None)
    assert not results[0]["ok"]
    assert not TelegramAccount.objects.exists()


def zip_upload(entries, name="accounts.zip"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in entries:
            archive.writestr(filename, data)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="application/zip")


def test_browser_imports_zip_with_40_nested_files(client, django_user_model, session_bytes):
    admin = django_user_model.objects.create_superuser("zip-admin", password="x")
    client.force_login(admin)
    entries = [(f"export/{i}/{file.name}", file.read())
               for i in range(20) for file in pair(str(71000000000 + i), session_bytes)]
    entries += [("README.txt", b"ignored"), ("__MACOSX/._account.json", b"ignored")]
    response = client.post(reverse("admin:accounts_telegramaccount_import_tdata"),
                           {"files": [zip_upload(entries)], "tags": "zip"})
    assert response.status_code == 200
    assert len(response.context["results"]) == 20
    assert all(row["ok"] for row in response.context["results"])
    assert TelegramAccount.objects.count() == 20
    assert all(account.tags.filter(name="zip").exists()
               for account in TelegramAccount.objects.all())
    assert b'accept=".json,.session,.zip"' in response.content


def test_zip_and_loose_files_share_pairing_and_duplicate_checks(session_bytes):
    loose_json, archived_session = pair("split", session_bytes)
    duplicate_json, duplicate_session = pair("duplicate", session_bytes)
    archive = zip_upload([
        ("nested/" + archived_session.name, archived_session.read()),
        ("first/" + duplicate_json.name, duplicate_json.read()),
        ("second/" + duplicate_json.name, b"{}"),
        ("second/" + duplicate_session.name, duplicate_session.read()),
        ("unpaired.json", b"{}"),
    ])
    results = import_tdata_accounts_from_uploads([archive, loose_json], None)
    assert len(results) == 3
    assert sum(row["ok"] for row in results) == 1
    assert TelegramAccount.objects.get().phone_number == "+split"


@pytest.mark.parametrize("entries", [
    [("../outside.json", b"{}")],
    [("/absolute.session", b"session")],
    [("README.txt", b"no account files")],
])
def test_invalid_zip_does_not_stop_loose_pairs(entries, session_bytes):
    results = import_tdata_accounts_from_uploads(
        [zip_upload(entries)] + pair("valid", session_bytes), None)
    assert len(results) == 2
    assert not results[0]["ok"]
    assert results[1]["ok"]
    assert TelegramAccount.objects.count() == 1


def test_corrupt_zip_is_reported(session_bytes):
    results = import_tdata_accounts_from_uploads(
        [SimpleUploadedFile("broken.zip", b"not a zip")]
        + pair("valid", session_bytes), None)
    assert not results[0]["ok"]
    assert results[1]["ok"]


@pytest.mark.parametrize("limit_name,limit", [
    ("_ARCHIVE_MAX_FILES", 1),
    ("_ARCHIVE_MAX_BYTES", 20),
    ("_ARCHIVE_MAX_FILE_BYTES", 20),
])
def test_archive_limits_reject_whole_archive(monkeypatch, session_bytes, limit_name, limit):
    from accounts.services import tdata_import

    monkeypatch.setattr(tdata_import, limit_name, limit)
    archive = zip_upload([(file.name, file.read()) for file in pair("oversized", session_bytes)])
    results = import_tdata_accounts_from_uploads([archive], None)
    assert len(results) == 1
    assert not results[0]["ok"]
    assert not TelegramAccount.objects.exists()
