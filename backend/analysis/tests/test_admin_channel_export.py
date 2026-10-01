"""Кнопка «⬇ CSV» у довіднику каналів: експортує рівно те, що відфільтровано."""
import pytest
from django.contrib.auth.models import User

from analysis.models import Channel, Region

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(client, settings):
    settings.SECURE_SSL_REDIRECT = False
    user = User.objects.create_superuser("root", password="x")
    client.force_login(user)
    return user


@pytest.fixture
def channels():
    region = Region.objects.create(name="Дагестан")
    Channel.objects.create(url="https://t.me/big", username="big", title="Великий",
                           subscribers=50_000, region_subject=region, topics=["новини"])
    Channel.objects.create(url="https://t.me/small", username="small", title="Малий",
                           subscribers=500)
    # юзернейм-інвайт: «+» не має перетворитись на «'+» — це ключ для склейки
    Channel.objects.create(url="https://t.me/+hash", username="+hash", title="Інвайт",
                           subscribers=20_000)


def _rows(response):
    body = b"".join(response.streaming_content).decode("utf-8").lstrip("﻿")
    return [line for line in body.split("\r\n") if line]


def test_button_on_changelist(client, staff):
    html = client.get("/admin/analysis/channel/").content.decode()
    assert "/admin/analysis/channel/export/" in html


def test_export_respects_filters(client, staff, channels):
    rows = _rows(client.get("/admin/analysis/channel/export/",
                            {"subscribers__range__gte": "10000"}))
    assert rows[0].startswith("id;platform;username;title;subscribers")
    assert len(rows) == 3                        # шапка + два канали від 10k
    assert "Малий" not in "\n".join(rows)
    assert "Дагестан" in rows[1]                 # регіон іде назвою, не id
    assert "новини" in rows[1]


def test_export_keeps_invite_username_intact(client, staff, channels):
    rows = _rows(client.get("/admin/analysis/channel/export/",
                            {"subscribers__range__gte": "10000"}))
    assert any(row.split(";")[2] == "+hash" for row in rows[1:])


def test_export_defuses_formula_in_free_text(client, staff):
    Channel.objects.create(url="https://t.me/evil", username="evil",
                           title="=HYPERLINK(\"http://x\")", subscribers=10_000)
    import csv
    row = next(csv.reader(_rows(client.get("/admin/analysis/channel/export/"))[1:],
                          delimiter=";"))
    assert row[3] == "'=HYPERLINK(\"http://x\")"     # апостроф глушить формулу


def test_export_denied_for_anonymous(client, settings):
    settings.SECURE_SSL_REDIRECT = False
    resp = client.get("/admin/analysis/channel/export/")
    assert resp.status_code == 302 and "/admin/login/" in resp["Location"]
