"""Група «Публікації»: розділ публікацій в адмінці (профілі + журнал) без
решти прав «Продвинутого аналітика». Свої профілі — за owner.

view_analysistask / view_tag — лише для автодоповнення полів «Задача» і
«Теги» у формі профілю (autocomplete вимагає view на цільову модель); задачі
всередині й так лише свої. Дзеркало в MCP — publish_config_* (mcp:write,
створення — mcp:create).
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUP_NAME = "Публікації"
CODENAMES = [
    "view_publishconfig", "add_publishconfig", "change_publishconfig", "delete_publishconfig",
    "view_publishedevent", "change_publishedevent", "delete_publishedevent",
    "view_analysistask", "view_tag", "view_region",
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
        ("analysis", "0086_group_advanced_analyst"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(create_group, delete_group)]
