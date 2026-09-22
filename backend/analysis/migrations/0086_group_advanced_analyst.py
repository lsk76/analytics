"""Група «Продвинутий аналітик»: створює й веде СВОЇ дослідження, канали
довідника, джерела (з підписками), збори, події, Telegram-акаунти та
публікації в чат (профілі публікації + журнал опублікованого).

Група в проді вже існувала (задачі/джерела/акаунти/публікація) — тут права
ДОДАЮТЬСЯ до наявних (union, не set), щоб не зняти видане руками. Чого нема
навмисно: delete_channel (довідник спільний), Proxy (пул із кредами), Setting,
TagCategory-add/delete (нова категорія ламає адмін-фасет без коду).
Видимість усередині — та сама, що для всіх не-суперюзерів: свої задачі
(`owner`), свої + спільні акаунти (`visible_to`). Дзеркало в MCP — роль
`analyst` (mcpauth.McpRole, скоуп mcp:create).
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUP_NAME = "Продвинутий аналітик"
PERMS = {
    "analysis": [
        # дослідження
        "view_analysistask", "add_analysistask", "change_analysistask",
        # довідник каналів (без delete — спільний)
        "view_channel", "add_channel", "change_channel",
        # джерела й підписки задач
        "view_source", "add_source", "change_source",
        "view_sourcesubscription", "add_sourcesubscription", "change_sourcesubscription",
        "delete_sourcesubscription",
        # whitelist чатів моніторингу
        "view_monitorchat", "add_monitorchat", "change_monitorchat", "delete_monitorchat",
        # збори
        "view_researchrun", "add_researchrun", "change_researchrun",
        # події: додати за посиланням, схвалити/відхилити, теги
        "view_event", "add_event", "change_event",
        "view_post",
        # довідники — лише читати (теги/регіони канонізує код)
        "view_tag", "view_tagcategory", "view_region",
        # публікації в Telegram-чат: профілі (свої, за owner) + журнал опублікованого
        "view_publishconfig", "add_publishconfig", "change_publishconfig", "delete_publishconfig",
        "view_publishedevent", "change_publishedevent", "delete_publishedevent",
    ],
    "accounts": [
        "view_telegramaccount", "add_telegramaccount", "change_telegramaccount",
        "view_accounttag", "add_accounttag",
        "view_warmupjob", "view_testbotjob",
    ],
}


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    for label in PERMS:
        app_config = apps.get_app_config(label)
        app_config.models_module = True      # свіжа БД: Permission ще не створені
        create_permissions(app_config, apps=apps, verbosity=0)
    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    for label, codenames in PERMS.items():
        group.permissions.add(*Permission.objects.filter(content_type__app_label=label,
                                                         codename__in=codenames))


def revoke(apps, schema_editor):
    # групу не видаляємо (вона була до міграції) — знімаємо лише додане тут
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group = Group.objects.filter(name=GROUP_NAME).first()
    if group:
        for label, codenames in PERMS.items():
            group.permissions.remove(*Permission.objects.filter(
                content_type__app_label=label, codename__in=codenames))


class Migration(migrations.Migration):
    dependencies = [
        ("analysis", "0085_source_fields_to_directory"),
        ("accounts", "0017_account_resolve_failures"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(grant, revoke)]
