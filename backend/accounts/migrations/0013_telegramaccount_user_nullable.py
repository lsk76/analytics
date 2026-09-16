import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """user стає власником: необов'язковий, SET_NULL (видалення користувача не знищує акаунт)."""

    dependencies = [
        ("accounts", "0012_group_telegram_accounts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="telegramaccount",
            name="user",
            field=models.ForeignKey(blank=True, help_text="Порожньо — спільний акаунт (бачать усі). Власник бачить лише свої акаунти й спільні; суперюзер — усі.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="telegram_accounts", to=settings.AUTH_USER_MODEL, verbose_name="Власник"),
        ),
    ]
