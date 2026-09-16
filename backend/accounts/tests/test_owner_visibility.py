"""Видимість Telegram-акаунтів/ботів за власником (TelegramAccount.user):
суперюзер — усі; решта — свої + без власника; чужі — 404 навіть за прямим URL."""
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from accounts.models import TelegramAccount, TelegramBot, WarmUpJob

pytestmark = pytest.mark.django_db


@pytest.fixture
def users(django_user_model):
    """alice/bob — члени групи «Telegram-акаунти» (міграція 0012)."""
    admin = django_user_model.objects.create_superuser("root", password="x")
    alice = django_user_model.objects.create_user("alice", password="x", is_staff=True)
    bob = django_user_model.objects.create_user("bob", password="x", is_staff=True)
    group = Group.objects.get(name="Telegram-акаунти")
    for u in (alice, bob):
        u.groups.add(group)
    return admin, alice, bob


def test_group_grants_only_accounts_section(client, users):
    _, alice, _ = users
    assert not alice.has_perm("accounts.delete_telegramaccount")
    assert not alice.has_perm("accounts.view_proxy")
    client.force_login(alice)
    apps = client.get(reverse("admin:index")).context["app_list"]
    assert [a["app_label"] for a in apps] == ["accounts"]
    assert "Proxy" not in {m["object_name"] for m in apps[0]["models"]}


@pytest.fixture
def accounts(users):
    admin, alice, bob = users
    mk = lambda name, phone, owner: TelegramAccount.objects.create(  # noqa: E731
        user=owner, name=name, phone_number=phone)
    return {
        "shared": mk("acc-shared", "+100", None),
        "alice": mk("acc-alice", "+200", alice),
        "bob": mk("acc-bob", "+300", bob),
    }


def test_visible_to(users, accounts):
    admin, alice, _ = users
    assert set(TelegramAccount.objects.visible_to(admin)) == set(accounts.values())
    assert set(TelegramAccount.objects.visible_to(alice)) == {accounts["shared"],
                                                             accounts["alice"]}


def test_account_changelist_scoped(client, users, accounts):
    _, alice, _ = users
    client.force_login(alice)
    html = client.get(reverse("admin:accounts_telegramaccount_changelist")).content.decode()
    assert "acc-shared" in html and "acc-alice" in html
    assert "acc-bob" not in html


def test_superuser_sees_all(client, users, accounts):
    admin, _, _ = users
    client.force_login(admin)
    html = client.get(reverse("admin:accounts_telegramaccount_changelist")).content.decode()
    assert all(n in html for n in ("acc-shared", "acc-alice", "acc-bob"))
    TelegramBot.objects.create(account=accounts["bob"], username="bob_bot")
    resp = client.get(reverse("admin:accounts_telegrambot_changelist"))
    assert resp.status_code == 200 and "bob_bot" in resp.content.decode()


def test_foreign_account_pages_404(client, users, accounts):
    _, alice, _ = users
    client.force_login(alice)
    bob_id = accounts["bob"].pk
    for name in ("authorize", "channels", "messages", "messages_poll", "create_bot"):
        url = reverse(f"admin:accounts_telegramaccount_{name}", args=[bob_id])
        assert client.get(url).status_code == 404, name
    # стандартна change-сторінка на «не існує» редіректить в індекс адмінки
    resp = client.get(reverse("admin:accounts_telegramaccount_change", args=[bob_id]))
    assert resp.status_code == 302 and "acc-bob" not in resp.content.decode()


def test_bots_and_jobs_follow_account(client, users, accounts):
    _, alice, _ = users
    TelegramBot.objects.create(account=accounts["shared"], username="shared_bot")
    TelegramBot.objects.create(account=accounts["alice"], username="alice_bot")
    bob_bot = TelegramBot.objects.create(account=accounts["bob"], username="bob_bot")
    WarmUpJob.objects.create(account=accounts["bob"], handles=["x"])
    client.force_login(alice)

    html = client.get(reverse("admin:accounts_telegrambot_changelist")).content.decode()
    assert "shared_bot" in html and "alice_bot" in html and "bob_bot" not in html
    assert client.get(reverse("admin:accounts_telegrambot_edit",
                              args=[bob_bot.pk])).status_code == 404
    html = client.get(reverse("admin:accounts_warmupjob_changelist")).content.decode()
    assert "acc-bob" not in html


def test_new_account_by_owner_is_owned(client, users):
    _, alice, _ = users
    client.force_login(alice)
    resp = client.post(reverse("admin:accounts_telegramaccount_add"), {
        "name": "new", "phone_number": "+400",
        "api_id": "1", "api_hash": "h", "is_active": "on",
        "spam_status": "unknown", "user": "",   # спроба лишити спільним — ігнорується
    })
    assert resp.status_code == 302, resp.content.decode()[:2000]
    assert TelegramAccount.objects.get(phone_number="+400").user == alice
