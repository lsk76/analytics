from django.db import migrations
from django.db.models import F


def merge_owner_into_user(apps, schema_editor):
    """user := owner дослівно (разом із NULL): видимість лишається рівно такою, як була
    з owner. Колишній «хто завів» у user власником не вважаємо."""
    TelegramAccount = apps.get_model("accounts", "TelegramAccount")
    TelegramAccount.objects.update(user=F("owner"))


class Migration(migrations.Migration):
    dependencies = [("accounts", "0013_telegramaccount_user_nullable")]
    operations = [migrations.RunPython(merge_owner_into_user, migrations.RunPython.noop)]
