from django.db import migrations


def merge_owner_into_user(apps, schema_editor):
    """owner → user. Де owner не задано: акаунти, заведені суперюзером, стають спільними
    (як і було з owner=NULL); заведені звичайним користувачем — лишаються його."""
    TelegramAccount = apps.get_model("accounts", "TelegramAccount")
    for acc in TelegramAccount.objects.select_related("user"):
        if acc.owner_id:
            acc.user_id = acc.owner_id
        elif acc.user_id and acc.user.is_superuser:
            acc.user_id = None
        else:
            continue
        acc.save(update_fields=["user"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0013_telegramaccount_user_nullable")]
    operations = [migrations.RunPython(merge_owner_into_user, migrations.RunPython.noop)]
