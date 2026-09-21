"""Простий інтерфейс: логін, видимість як в адмінці, рендер секції."""
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from analysis.models import AnalysisTask, Event, Region
from analysis.tests.factories import SourceFactory, SubscriptionFactory, TaskFactory
from simpleui.services import status
from simpleui.services.periods import Period

pytestmark = pytest.mark.django_db


@pytest.fixture
def users():
    U = get_user_model()
    return (U.objects.create_superuser("root", "r@x", "pw-pw-pw-pw-pw"),
            U.objects.create_user("owner", "o@x", "pw-pw-pw-pw-pw", is_staff=True),
            U.objects.create_user("other", "t@x", "pw-pw-pw-pw-pw", is_staff=True))


@pytest.fixture
def task(users):
    _, owner, _ = users
    t = TaskFactory(owner=owner, display_name="Інформпростір регіонів")
    region = Region.objects.create(name="Дагестан", population=3_000_000)
    for i in range(3):
        Event.objects.create(task=t, event_date=date.today() - timedelta(days=i),
                             region_subject=region, summary=f"Подія {i}. Далі опис.",
                             review_status=Event.REVIEW_APPROVED)
    Event.objects.create(task=t, event_date=date.today(), summary="Не схвалено",
                         review_status=Event.REVIEW_PENDING)
    return t


def test_anonymous_redirects_to_admin_login(client):
    r = client.get("/app/")
    assert r.status_code == 302 and r["Location"].startswith("/admin/login/")


def test_visibility_matches_admin(client, users, task):
    root, owner, other = users
    client.force_login(other)
    assert task.human_name.encode() not in client.get("/app/").content
    assert client.get(f"/app/{task.id}/").status_code == 404
    client.force_login(owner)
    assert task.human_name.encode() in client.get("/app/").content
    assert client.get(f"/app/{task.id}/").status_code == 200
    client.force_login(root)
    assert client.get(f"/app/{task.id}/").status_code == 200


def test_section_shows_only_approved_and_hides_internals(client, users, task):
    client.force_login(users[1])
    r = client.get(f"/app/{task.id}/?period=month")
    body = r.content.decode()
    assert "Подія 0." in body and "Не схвалено" not in body
    for word in ("prescreen_prompt", "review_status", "awaiting_agent", "task_id"):
        assert word not in body           # внутрішні ідентифікатори назовні не йдуть
    assert "Налаштування" in body         # лінк на окрему сторінку
    r = client.get(f"/app/{task.id}/settings/")
    body = r.content.decode()
    assert "Стежимо за" in body           # речення людською мовою
    assert "Як працює збір" in body and "Назва для користувача" in body   # усі налаштування
    assert "Скопіювати" in body           # фрази для асистента
    assert "<input" not in body           # нічого не редагується


def test_event_card_and_collect_stub(client, users, task):
    client.force_login(users[1])
    e = Event.objects.filter(task=task, review_status="approved").first()
    assert client.get(f"/app/{task.id}/event/{e.id}/").status_code == 200
    pending = Event.objects.get(summary="Не схвалено")
    assert client.get(f"/app/{task.id}/event/{pending.id}/").status_code == 404
    r = client.get(f"/app/{task.id}/collect/")
    assert r.status_code == 200 and "Запустити" in r.content.decode()


def test_status_infospace_live_and_problem(task):
    now = timezone.now()
    src = SourceFactory(last_ok_at=now - timedelta(minutes=10))
    SubscriptionFactory(task=task, source=src)
    st = status.task_status(task, now)
    assert st.state == status.LIVE and st.week == 3 and st.today == 1
    src.last_ok_at = now - timedelta(hours=10)
    src.save()
    assert status.task_status(task, now).state == status.PROBLEM
    task.is_active = False
    task.save()
    assert status.task_status(task, now).state == status.STOPPED


def test_period_gran():
    assert Period("week", date(2026, 9, 15), date(2026, 9, 21)).gran == "day"
    assert Period("quarter", date(2026, 6, 23), date(2026, 9, 21)).gran == "week"


def test_human_name_fallback():
    t = AnalysisTask(name="tech", display_name="")
    assert t.human_name == "tech"
