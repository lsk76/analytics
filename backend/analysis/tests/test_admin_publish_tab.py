"""Розділ «Публікації» в адмінці для звичайного (не-супер) користувача:
вкладка дослідження, пункт навбара, свої профілі, чужі дослідження не світяться."""
import pytest
from django.contrib.auth.models import Group, User

from analysis.models import PublishConfig

from .factories import TaskFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def analyst(client):
    user = User.objects.create_user("ann", password="x", is_staff=True)
    user.groups.add(Group.objects.get(name="Продвинутий аналітик"))   # analysis.0086
    client.force_login(user)
    return user


def test_group_from_migration_has_publish_rights(analyst):
    for p in ("analysis.view_publishconfig", "analysis.add_publishconfig",
              "analysis.change_publishconfig", "analysis.delete_publishconfig",
              "analysis.view_publishedevent", "analysis.add_analysistask",
              "analysis.add_channel", "accounts.add_telegramaccount"):
        assert analyst.has_perm(p), p
    assert not analyst.has_perm("analysis.delete_channel")


def test_publications_tab_on_home_card_and_navbar(client, analyst, settings):
    settings.SECURE_SSL_REDIRECT = False
    mine = TaskFactory(slug="mine", owner=analyst)
    TaskFactory(slug="foreign", name="Чуже дослідження")
    html = client.get("/admin/").content.decode()
    assert f"/admin/analysis/publishconfig/?task__id__exact={mine.id}" in html   # картка
    assert 'href="/admin/analysis/publishconfig/"' in html                        # навбар
    assert "Чуже дослідження" not in html


def test_publishconfig_list_is_scoped_and_prefills_task(client, analyst, settings):
    settings.SECURE_SSL_REDIRECT = False
    mine = TaskFactory(slug="mine", owner=analyst)
    other = User.objects.create_user("bob", password="x", is_staff=True)
    foreign = TaskFactory(slug="foreign", name="Чуже дослідження", owner=other)
    PublishConfig.objects.create(name="Мій профіль", owner=analyst, task=mine, chat_id="-1")
    PublishConfig.objects.create(name="Чужий профіль", owner=other, task=foreign, chat_id="-2")

    html = client.get(f"/admin/analysis/publishconfig/?task__id__exact={mine.id}").content.decode()
    assert "Мій профіль" in html and "Чужий профіль" not in html
    assert "Чуже дослідження" not in html            # фільтр «Дослідження» — лише свої
    html = client.get("/admin/analysis/publishconfig/").content.decode()
    assert "Мій профіль" in html and "Чужий профіль" not in html

    resp = client.get(f"/admin/analysis/publishconfig/add/?_changelist_filters=task__id__exact%3D{mine.id}")
    assert resp.status_code == 200
    assert resp.context["adminform"].form.initial.get("task") == mine.id
    # чужий профіль за прямим id — 404/редирект, не сторінка
    foreign_cfg = PublishConfig.objects.get(name="Чужий профіль")
    assert client.get(f"/admin/analysis/publishconfig/{foreign_cfg.id}/change/").status_code != 200
