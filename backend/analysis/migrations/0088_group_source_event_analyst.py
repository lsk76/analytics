"""Група «Аналітик джерел і подій»: бачити/додавати/правити джерела (Source +
підписки задач) і події (додати за посиланням, схвалити/відхилити, теги),
плюс перегляд задач/каналів/чатів/довідників для форм і вкладок. Без прав на
створення досліджень, акаунти й публікації — це окремі групи
(«Продвинутий аналітик», «Telegram-акаунти», «Публікації»). Ті самі права
перевіряє MCP (mcp_api/perms.py)."""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUP_NAME = "Аналітик джерел і подій"
CODENAMES = [
    "view_source", "add_source", "change_source",
    "view_sourcesubscription", "add_sourcesubscription", "change_sourcesubscription",
    "delete_sourcesubscription",
    "view_event", "add_event", "change_event",
    "view_analysistask", "view_channel", "view_monitorchat", "view_researchrun", "view_post",
    "view_tag", "view_tagcategory", "view_region",
]


def create_group(apps, schema_editor):
    app_config = apps.get_app_config("analysis")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    group.permissions.add(*Permission.objects.filter(content_type__app_label="analysis",
                                                     codename__in=CODENAMES))


def delete_group(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("analysis", "0087_group_publications"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(create_group, delete_group)]
