"""«Додати подію за посиланням»: fetch t.me-embed/сайт → скрін → Post → Event."""
from datetime import datetime, timezone
from unittest import mock

import pytest

from analysis.models import Event, Post, Region, Tag, TagCategory
from analysis.services import event_by_link as ebl
from analysis.tests.factories import TaskFactory

pytestmark = pytest.mark.django_db

TME_HTML = '''<div class="tgme_widget_message_author accent_color"><a class="tgme_widget_message_owner_name"
href="https://t.me/kemertop"><span dir="auto">Кузбасс Топ</span></a></div>
<div class="tgme_widget_message_text js-message_text" dir="auto">Бензин в Кузбассе снова подорожал.<br/><br/>АИ-92 +0,9%.</div>
<time datetime="2026-09-21T10:54:09+00:00" class="time">13:54</time>'''


def _resp(text, status=200):
    r = mock.Mock(); r.text = text; r.status_code = status
    r.raise_for_status = mock.Mock()
    return r


def test_fetch_telegram_embed():
    with mock.patch.object(ebl.httpx, "get", return_value=_resp(TME_HTML)):
        f = ebl.fetch("https://t.me/s/kemertop/26887?foo=1")
    assert f.url == "https://t.me/kemertop/26887"
    assert "Бензин" in f.text and "\n" in f.text and "<br" not in f.text
    assert f.posted_at == datetime(2026, 9, 21, 10, 54, 9, tzinfo=timezone.utc)
    assert f.channel.username == "kemertop" and f.channel.title == "Кузбасс Топ"


def test_fetch_telegram_private_is_human_error():
    with mock.patch.object(ebl.httpx, "get", return_value=_resp("<html>nothing</html>")):
        with pytest.raises(ebl.LinkError, match="закритий"):
            ebl.fetch("https://t.me/private_chan/5")


def test_fetch_rejects_non_url():
    with pytest.raises(ebl.LinkError):
        ebl.fetch("kemertop/26887")


def test_create_event_fills_fields_and_is_idempotent():
    task = TaskFactory(geo_enabled=True)
    TagCategory.objects.create(key="campaign", label="Виборча кампанія", closed=True)
    Tag.objects.create(name="фальсифікації", category="campaign")
    task.tag_categories.set(TagCategory.objects.filter(key="campaign"))
    region = Region.objects.create(name="Кемеровська область", population=2_500_000)
    verdict = {"relevant": True, "summary": "Бензин у Кузбасі подорожчав.",
               "signature": "ціни на бензин", "region": "Кемеровська область",
               "tags": {"campaign": ["фальсифікації"]}}
    with mock.patch.object(ebl.httpx, "get", return_value=_resp(TME_HTML)), \
         mock.patch.object(ebl, "screen", return_value=verdict), \
         mock.patch("analysis.services.stages.resolve_region", return_value=(region, "")):
        ev, created = ebl.create_event(task, "https://t.me/kemertop/26887", user=None)
        ev2, created2 = ebl.create_event(task, "https://t.me/kemertop/26887", user=None)
    assert created and not created2 and ev.id == ev2.id
    assert ev.review_status == "approved" and ev.summary.startswith("Бензин")
    assert ev.region_subject == region and ev.event_date.isoformat() == "2026-09-21"
    assert list(ev.tags.values_list("name", flat=True)) == ["фальсифікації"]
    post = Post.objects.get(task=task, url="https://t.me/kemertop/26887")
    assert post.event_id == ev.id and post.stage == "done" and post.channel.username == "kemertop"
    assert Event.objects.filter(task=task).count() == 1


def test_admin_view_requires_study_and_redirects(client, django_user_model):
    root = django_user_model.objects.create_superuser("root", "r@x", "pw-pw-pw-pw-pw")
    client.force_login(root)
    task = TaskFactory()
    r = client.get(f"/admin/analysis/event/add-by-link/?task={task.id}")
    assert r.status_code == 200 and "Створити подію".encode() in r.content
    ev = Event.objects.create(task=task, summary="x", review_status="approved")
    with mock.patch("analysis.services.event_by_link.create_event", return_value=(ev, True)):
        r = client.post("/admin/analysis/event/add-by-link/",
                        {"task": task.id, "url": "https://t.me/kemertop/1"})
    assert r.status_code == 302 and r["Location"].endswith(f"/admin/analysis/event/{ev.id}/change/?task={task.id}")
    r = client.post("/admin/analysis/event/add-by-link/", {"task": task.id, "url": ""})
    assert r.status_code == 200 and "Вставте посилання".encode() in r.content
