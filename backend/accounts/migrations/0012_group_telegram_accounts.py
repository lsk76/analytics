"""Група «Telegram-акаунти»: доступ в адмінці лише до розділу акаунтів.

Видимість усередині — за власником TelegramAccount.user (свої + без власника). Навмисно БЕЗ:
- delete_telegramaccount — інакше член групи міг би видалити СПІЛЬНИЙ акаунт;
- Proxy — спільний пул із кредами, не звужується за власником.
Статус «Персонал» (is_staff) група дати не може — ставиться користувачу окремо.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUP_NAME = "Telegram-акаунти"
CODENAMES = [
    "view_telegramaccount", "add_telegramaccount", "change_telegramaccount",
    "view_telegrambot", "change_telegrambot",
    "view_accounttag", "add_accounttag",
    "view_warmupjob", "change_warmupjob",
    "view_testbotjob", "change_testbotjob",
]


def create_group(apps, schema_editor):
    # на свіжій БД (тести) Permission-рядки ще не створені post_migrate-ом
    app_config = apps.get_app_config("accounts")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    group.permissions.set(Permission.objects.filter(content_type__app_label="accounts",
                                                    codename__in=CODENAMES))


def delete_group(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0011_telegramaccount_owner"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(create_group, delete_group)]
