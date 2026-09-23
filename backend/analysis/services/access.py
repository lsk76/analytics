"""ЄДИНЕ місце, де вирішується, що користувач бачить: адмінка й MCP беруть
правило звідси, а не пишуть свій фільтр.

Раніше правил було троє (міксини адмінки, інлайн-фільтри в окремих ModelAdmin,
`mcp_api/common.scope_*`) — і вони розходилися: MCP ховав чужі збори, чати,
підписки й джерела, а адмінка ті самі рядки показувала всім, хто мав
`view_*`. Тепер правило одне на модель (`RULES`), а обидва шари його лише
застосовують:

    access.visible(ResearchRun.objects.all(), request.user)

Суперюзер бачить усе. `unrestricted=True` — локальний stdio-MCP на машині
власника (там користувача немає взагалі).

Правило `PUBLIC` = спільний довідник (канали, теги, регіони, налаштування):
доступ до них обмежують ПРАВА (`view_channel`…), а не власність — рядок
довідника спільний за задумом, його збагачують усі.

Тест `test_access_rules.py` не дасть зареєструвати в адмінці модель без
правила тут і звіряє, що адмінка й MCP віддають однакові набори.
"""
from django.db.models import Q

PUBLIC = "public"      # видно всім, у кого є право на модель


def _own_tasks(user):
    from analysis.models import AnalysisTask
    return AnalysisTask.objects.filter(owner=user).values("pk")


def _visible_accounts(user):
    from accounts.models import TelegramAccount
    return TelegramAccount.objects.visible_to(user).values("pk")


def _sources_of_own_tasks(qs, user):
    """Джерело видиме, якщо на нього підписана хоч одна МОЯ задача.

    Підписку враховуємо будь-яку, не лише активну: вимкнена підписка — це
    «тимчасово не збираємо», а не «чуже джерело»; інакше джерело зникало б із
    очей власника рівно тоді, коли він його вимикає.
    """
    from analysis.models import SourceSubscription
    return qs.filter(id__in=SourceSubscription.objects.filter(
        task__owner=user).values("source_id"))


def _proxies(qs, user):
    """Проксі, з якими людина має справу: вільні або на її акаунтах.

    Чужу проксі не можна ні побачити, ні «полагодити» (ремонт іде реальною
    сесією акаунта на ній), вільну — можна призначити своєму акаунту.
    """
    from accounts.models import TelegramAccount
    mine = TelegramAccount.objects.visible_to(user).values("proxy_id")
    return qs.filter(Q(accounts__isnull=True) | Q(id__in=mine)).distinct()


# Модель → як звузити вибірку до «свого». PUBLIC = спільний довідник.
RULES = {
    # дослідження і все, що йому належить
    "analysis.AnalysisTask": lambda qs, u: qs.filter(owner=u),
    "analysis.Event": lambda qs, u: qs.filter(task__owner=u),
    "analysis.Post": lambda qs, u: qs.filter(task__owner=u),
    "analysis.ResearchRun": lambda qs, u: qs.filter(task__owner=u),
    "analysis.CollectChunk": lambda qs, u: qs.filter(task__owner=u),
    "analysis.MonitorChat": lambda qs, u: qs.filter(task__owner=u),
    "analysis.SourceSubscription": lambda qs, u: qs.filter(task__owner=u),
    "analysis.ResearchRubric": lambda qs, u: qs.filter(task__owner=u),
    "analysis.ChannelDailyStat": lambda qs, u: qs.filter(task__owner=u),
    "analysis.Source": _sources_of_own_tasks,
    # публікація
    "analysis.PublishConfig": lambda qs, u: qs.filter(owner=u),
    "analysis.PublishedEvent": lambda qs, u: qs.filter(config__owner=u),
    # Telegram-акаунти й усе, що на них висить
    "accounts.TelegramAccount": lambda qs, u: qs.visible_to(u),
    "accounts.TelegramBot": lambda qs, u: qs.filter(account__in=_visible_accounts(u)),
    "accounts.WarmUpJob": lambda qs, u: qs.filter(account__in=_visible_accounts(u)),
    "accounts.TestBotJob": lambda qs, u: qs.filter(account__in=_visible_accounts(u)),
    "accounts.Proxy": _proxies,
    # спільні довідники: межа — права, не власність
    "analysis.Channel": PUBLIC, "analysis.Tag": PUBLIC, "analysis.TagAlias": PUBLIC,
    "analysis.TagCategory": PUBLIC, "analysis.Region": PUBLIC, "analysis.RegionAlias": PUBLIC,
    "analysis.Setting": PUBLIC, "analysis.TelezipSlot": PUBLIC,
    "accounts.AccountTag": PUBLIC,
    # службове: доступ дають лише стокові права Django (у групах їх немає)
    "auth.User": PUBLIC, "auth.Group": PUBLIC, "auth.Permission": PUBLIC,
    "analysis.UserProfile": PUBLIC,
    "mcpauth.McpRole": PUBLIC, "mcpauth.McpClient": PUBLIC, "mcpauth.McpToken": PUBLIC,
    "mcpauth.McpAuthCode": PUBLIC, "mcpauth.McpAuditLog": PUBLIC,
    "mcpauth.McpAuthRequest": PUBLIC,
}


class NoRule(Exception):
    """Модель без правила видимості — це не «видно всім», а незакрите питання."""


def rule_for(model) -> object:
    label = model._meta.label
    if label not in RULES:
        raise NoRule(f"{label}: немає правила видимості — додай рядок у "
                     "analysis/services/access.py::RULES (PUBLIC, якщо це спільний довідник)")
    return RULES[label]


def visible(qs, user, *, unrestricted: bool = False):
    """Звузити вибірку до видимого цьому користувачу.

    `unrestricted=True` — локальний stdio-MCP (машина власника, юзера немає).
    Суперюзер і PUBLIC-моделі повертаються як є.
    """
    if unrestricted or (user is not None and user.is_superuser):
        return qs
    rule = rule_for(qs.model)
    if rule is PUBLIC:
        return qs
    if user is None or not user.is_authenticated:
        return qs.none()
    return rule(qs, user)
