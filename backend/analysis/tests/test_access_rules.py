"""Одне правило видимості на систему (`services/access.py`): кожна модель
адмінки його має, адмінка й MCP дають ОДНАКОВІ набори рядків.

Тест тримає інваріант «що бачу в адмінці — те саме бачу через MCP»: додавши
модель в адмінку без правила або написавши власний фільтр у ModelAdmin, його
ламаєш.
"""
import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.test import RequestFactory

from accounts.models import Proxy, TelegramAccount, WarmUpJob
from analysis.models import (AnalysisTask, Channel, CollectChunk, Event, MonitorChat, Post,
                             PublishConfig, PublishedEvent, ResearchRun, SourceSubscription)
from analysis.services import access
from analysis.services import mcp_api
from analysis.services.mcp_api import Actor, common

from .factories import SourceFactory, TaskFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_worlds(django_user_model):
    """Дві людини, у кожної своє дослідження з повним набором дочірніх рядків."""
    made = {}
    for name in ("ann", "bob"):
        u = django_user_model.objects.create_user(name, is_staff=True, password="x")
        u.user_permissions.set(Permission.objects.all())
        t = TaskFactory(slug=f"{name}-task", owner=u)
        ch = Channel.objects.create(username=f"{name}chan", title=f"Канал {name}")
        src = SourceFactory(url=f"https://{name}.example.org/rss.xml")
        sub = SourceSubscription.objects.create(task=t, source=src)
        acc = TelegramAccount.objects.create(name=f"Акаунт {name}", phone_number=f"+7000000{name[0]}1",
                                             user=u, proxy=Proxy.objects.create(
                                                 proxy_string=f"{name}.proxy:1:u:p"))
        cfg = PublishConfig.objects.create(name=f"Профіль {name}", chat_id=f"-100{name[0]}", owner=u, task=t)
        ev = Event.objects.create(task=t, event_date="2026-09-20", summary=f"подія {name}")
        made[name] = {
            "user": u, "task": t,
            AnalysisTask: t, Event: ev,
            Post: Post.objects.create(task=t, url=f"https://{name}.x/1", channel=ch),
            ResearchRun: ResearchRun.objects.create(task=t, date_from="2026-09-01", date_to="2026-09-02"),
            CollectChunk: CollectChunk.objects.create(task=t, date_from="2026-09-01", date_to="2026-09-02"),
            MonitorChat: MonitorChat.objects.create(task=t, channel=ch),
            SourceSubscription: sub,
            type(src): src,
            TelegramAccount: acc,
            WarmUpJob: WarmUpJob.objects.create(account=acc, handles=["x"]),
            Proxy: acc.proxy,
            PublishConfig: cfg,
            PublishedEvent: PublishedEvent.objects.create(config=cfg, event=ev, status="published"),
        }
    return made


def test_every_admin_model_has_a_rule():
    missing = []
    for model in admin.site._registry:
        try:
            access.rule_for(model)
        except access.NoRule:
            missing.append(model._meta.label)
    assert missing == [], f"моделі в адмінці без правила видимості: {missing}"


def test_no_model_admin_writes_its_own_visibility_filter():
    """Фільтр власника — лише у ScopedAdminMixin/AccountVisibilityAdminMixin."""
    import inspect
    from analysis.admin import ScopedAdminMixin
    from accounts.admin import AccountVisibilityAdminMixin
    allowed = {ScopedAdminMixin.get_queryset, AccountVisibilityAdminMixin.get_queryset}
    offenders = []
    for model, model_admin in admin.site._registry.items():
        fn = model_admin.__class__.__dict__.get("get_queryset")
        if fn is None or fn in allowed:
            continue
        src = inspect.getsource(fn)
        if "is_superuser" in src or "owner=request.user" in src:
            offenders.append(model._meta.label)
    assert offenders == [], f"власний фільтр видимості замість access.py: {offenders}"


@pytest.mark.parametrize("model", [AnalysisTask, Event, Post, ResearchRun, CollectChunk,
                                   MonitorChat, SourceSubscription, TelegramAccount, WarmUpJob,
                                   PublishConfig, PublishedEvent])
def test_admin_and_mcp_agree_on_every_model(two_worlds, model):
    ann, bob = two_worlds["ann"], two_worlds["bob"]
    model_admin = admin.site._registry[model]
    request = RequestFactory().get("/")
    request.user = ann["user"]
    admin_ids = set(model_admin.get_queryset(request).values_list("pk", flat=True))

    who = Actor(user=ann["user"], scopes=["mcp:read"], role="operator")
    from analysis.services.mcp_api.registry import _actor
    token = _actor.set(who)
    try:
        mcp_ids = set(common.scope(model.objects.all()).values_list("pk", flat=True))
    finally:
        _actor.reset(token)

    assert admin_ids == mcp_ids, f"{model._meta.label}: адмінка {admin_ids} ≠ MCP {mcp_ids}"
    assert ann[model].pk in admin_ids, "своє має бути видно"
    assert bob[model].pk not in admin_ids, "чуже видно не має"


def test_source_visible_through_own_subscription_only(two_worlds):
    from analysis.models import Source
    ann, bob = two_worlds["ann"], two_worlds["bob"]
    request = RequestFactory().get("/")
    request.user = ann["user"]
    ids = set(admin.site._registry[Source].get_queryset(request).values_list("pk", flat=True))
    assert ann[Source].pk in ids and bob[Source].pk not in ids
    # вимкнена підписка не ховає джерело від власника (інакше зникало б при вимкненні)
    SourceSubscription.objects.filter(task=ann["task"]).update(is_active=False)
    ids = set(admin.site._registry[Source].get_queryset(request).values_list("pk", flat=True))
    assert ann[Source].pk in ids


def test_proxy_visible_if_free_or_mine(two_worlds):
    ann, bob = two_worlds["ann"], two_worlds["bob"]
    free = Proxy.objects.create(proxy_string="free.proxy:1:u:p")
    request = RequestFactory().get("/")
    request.user = ann["user"]
    ids = set(admin.site._registry[Proxy].get_queryset(request).values_list("pk", flat=True))
    assert ids == {ann[Proxy].pk, free.pk}


def test_superuser_sees_everything_and_local_mcp_unrestricted(two_worlds):
    root = User.objects.create_superuser("root", password="x")
    request = RequestFactory().get("/")
    request.user = root
    assert admin.site._registry[Event].get_queryset(request).count() == 2
    assert common.scope(Event.objects.all()).count() == 2      # без who = Actor.local()


def test_anonymous_sees_nothing():
    from django.contrib.auth.models import AnonymousUser
    TaskFactory(slug="x")
    assert access.visible(AnalysisTask.objects.all(), AnonymousUser()).count() == 0
    assert access.visible(AnalysisTask.objects.all(), None).count() == 0
