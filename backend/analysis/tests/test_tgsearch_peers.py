"""Стрім tgsearch: чат читається за хешем, який ЦЕЙ акаунт уже здобув; резолв —
лише при першому контакті акаунта з чатом."""
import pytest

from analysis.models import Channel
from analysis.services import peers
from analysis.services import tgsearch_stages as tgs

pytestmark = pytest.mark.django_db


def test_entity_spec_prefers_per_account_hash():
    ch = Channel.objects.create(username="pub", title="p", tg_id=100,
                                raw_meta={"access_hash_by_acc": {"7": 999}})
    assert tgs._entity_spec(ch, 7) == {"channel_id": 100, "access_hash": 999}
    assert tgs._entity_spec(ch, 8) == {"username": "pub"}          # чужий хеш не береться
    assert tgs._entity_spec(ch) == {"username": "pub"}
    linked = Channel.objects.create(username="linked:parent", title="l")
    assert tgs._entity_spec(linked, 7) == {"linked_parent": "parent"}
    # ГЛОБАЛЬНИЙ хеш у raw_meta здобув ЧУЖИЙ акаунт, а access hash привʼязаний
    # до акаунта — тож для linked-групи він НЕ береться (прод 28.09: так
    # мовчки лягли 6 із 6 обшуканих обговорень, ChannelInvalidError). Резолв
    # батьківського каналу дає хеш під цей акаунт.
    linked.tg_id, linked.raw_meta = 200, {"access_hash": 5}
    assert tgs._entity_spec(linked, 7) == {"linked_parent": "parent"}
    # а хеш САМОГО цього акаунта — навпаки, головний шлях (резолву не платимо)
    linked.raw_meta = {"access_hash": 5, "access_hash_by_acc": {"7": 77}}
    assert tgs._entity_spec(linked, 7) == {"channel_id": 200, "access_hash": 77}


def test_apply_resolved_stores_under_account():
    ch = Channel.objects.create(username="pub", title="p")
    tgs._apply_resolved(ch, {"id": 100, "access_hash": 999}, 7)
    ch.refresh_from_db()
    assert ch.tg_id == 100 and ch.raw_meta["access_hash_by_acc"] == {"7": 999}
    assert peers.remember_peer(ch, 7, {"id": 100, "access_hash": 999}) is False   # без змін
    other = Channel.objects.create(username="dup", title="d")
    tgs._apply_resolved(other, {"id": 100, "access_hash": 1}, 8)      # tg_id зайнятий
    other.refresh_from_db()
    assert other.tg_id is None and other.raw_meta["access_hash_by_acc"] == {"8": 1}


def test_assign_accounts_prefers_hash_owner_then_resolver(django_user_model):
    from datetime import timedelta

    from django.utils import timezone

    from accounts.models import Proxy, TelegramAccount
    from analysis.models import MonitorChat
    from analysis.tests.factories import TaskFactory
    u = django_user_model.objects.create(username="op")
    accs = [TelegramAccount.objects.create(user=u, phone_number=f"+{i}", is_authenticated=True,
                                           proxy=Proxy.objects.create(proxy_string=f"h:{i}:u:p"))
            for i in (1, 2, 3)]
    a1, a2, a3 = accs
    a1.resolve_exhausted_until = timezone.now() + timedelta(hours=1); a1.save()
    task = TaskFactory()
    ch_known = Channel.objects.create(username="k", title="k", tg_id=5,
                                      raw_meta={"access_hash_by_acc": {str(a1.id): 7}})
    ch_new = Channel.objects.create(username="n", title="n")
    mc_k = MonitorChat.objects.create(task=task, channel=ch_known)
    mc_n = MonitorChat.objects.create(task=task, channel=ch_new)
    by_acc = tgs._assign_accounts([mc_k, mc_n])
    assert a1.id in by_acc and by_acc[a1.id] == [mc_k]              # хеш є → a1 попри вичерпаний резолв
    assert mc_n.tg_account_id in (a2.id, a3.id)                     # новий чат → лише хто резолвить


def test_search_caches_linked_resolve_under_account(monkeypatch, django_user_model):
    """Пошук у linked-групі: хеш із резолву кешується під акаунт, як у стрімі.

    Раніше `_run_search_account` викидав `resolved` — і кожен прохід платив
    резолв батьківського каналу заново (а він має добовий ліміт ~200).
    """
    from accounts.models import Proxy, TelegramAccount
    from analysis.models import MonitorChat
    from analysis.tests.factories import TaskFactory
    u = django_user_model.objects.create(username="op3")
    acc = TelegramAccount.objects.create(
        user=u, phone_number="+77", is_authenticated=True,
        proxy=Proxy.objects.create(proxy_string="h:7:u:p"))
    task = TaskFactory(pipeline="tgsearch", search_terms="едро")
    ch = Channel.objects.create(username="linked:parent", title="l")
    mc = MonitorChat.objects.create(task=task, channel=ch, tg_account=acc)
    assert tgs._entity_spec(ch, acc.id) == {"linked_parent": "parent"}

    class FakeAcc:
        def search(self, req, terms, **kw):
            assert req[0]["entity"] == {"linked_parent": "parent"}
            return [{"key": mc.id, "hits": [], "error": None,
                     "resolved": {"id": 555, "access_hash": 888}}]
    monkeypatch.setattr("accounts.services.registry.get", lambda _id: FakeAcc())

    out = []
    tgs._run_search_account(acc.id, [mc], ["едро"], None, 50, out)
    ch.refresh_from_db()
    assert ch.raw_meta["access_hash_by_acc"] == {str(acc.id): 888}
    # наступного проходу резолв уже не потрібен — ідемо прямо за хешем
    assert tgs._entity_spec(ch, acc.id) == {"channel_id": 555, "access_hash": 888}


def test_search_error_retries_soon_not_in_12h(monkeypatch, django_user_model):
    """Збій пошуку не має виводити чат із роботи на всі RESEARCH_EVERY.

    Помилка тут майже завжди транзієнтна (gateway у таймауті, акаунт у паузі),
    а раніше впалий чат отримував свіжий watermark і мовчки чекав 12 годин —
    саме так на проді 28.09 стояли всі обшукані linked-чати.
    """
    from django.utils import timezone

    from accounts.models import Proxy, TelegramAccount
    from analysis.models import MonitorChat
    from analysis.tests.factories import TaskFactory
    u = django_user_model.objects.create(username="op2")
    acc = TelegramAccount.objects.create(
        user=u, phone_number="+99", is_authenticated=True,
        proxy=Proxy.objects.create(proxy_string="h:9:u:p"))
    task = TaskFactory(pipeline="tgsearch", search_terms="едро", search_days=30)
    bad = MonitorChat.objects.create(
        task=task, channel=Channel.objects.create(username="b", title="b"), tg_account=acc)
    good = MonitorChat.objects.create(
        task=task, channel=Channel.objects.create(username="g", title="g"), tg_account=acc)

    def fake_search(by_acc, terms, since, limit, out):
        out.append((bad, [], "ChannelInvalidError: Invalid channel object"))
        out.append((good, [], None))
    monkeypatch.setattr(tgs, "_search_all_sync", fake_search)

    assert tgs.tgs_search_once(task) is True
    bad.refresh_from_db(); good.refresh_from_db()
    now = timezone.now()
    # впалий: наступна спроба вже через RETRY_AFTER
    assert bad.last_searched_at < now - tgs.RESEARCH_EVERY + tgs.RETRY_AFTER + tgs.RETRY_AFTER
    assert bad.last_searched_at > now - tgs.RESEARCH_EVERY
    assert "ChannelInvalidError" in bad.notes
    # здоровий (просто нуль влучень): звичайні 12 годин
    assert good.last_searched_at > now - tgs.RETRY_AFTER
