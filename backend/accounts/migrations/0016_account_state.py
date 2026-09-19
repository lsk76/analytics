from django.db import migrations, models


def _deauth_state(apps, schema_editor):
    TelegramAccount = apps.get_model("accounts", "TelegramAccount")
    TelegramAccount.objects.filter(is_authenticated=False).update(state="deauthorized")


class Migration(migrations.Migration):
    """Стан акаунта для gateway (docs/tg-gateway-plan.md §3.4). Усе адитивне з
    дефолтами: накочується на живий стек."""

    dependencies = [("accounts", "0015_remove_owner_unique_phone")]

    operations = [
        migrations.AddField(
            model_name="telegramaccount", name="state",
            field=models.CharField(
                choices=[("ready", "Готовий"), ("cooldown", "Пауза (cooldown)"),
                         ("needs_proxy", "Потрібна проксі"), ("deauthorized", "Розлогінений"),
                         ("banned", "Забанений")],
                default="ready", max_length=16, verbose_name="Стан"),
        ),
        migrations.AddField(
            model_name="telegramaccount", name="cooldown_until",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Пауза до"),
        ),
        migrations.AddField(
            model_name="telegramaccount", name="transport_failures",
            field=models.PositiveIntegerField(
                default=0, help_text="Проксі не зʼєднує / таймаут. Скидається успішною операцією.",
                verbose_name="Транспортних збоїв поспіль"),
        ),
        migrations.AddField(
            model_name="telegramaccount", name="resolve_exhausted_until",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text="«No user has X as username» — добовий ліміт резолву; акаунт "
                          "лишається придатним для операцій без резолву.",
                verbose_name="Резолв юзернеймів вичерпано до"),
        ),
        migrations.AddField(
            model_name="telegramaccount", name="last_ok_at",
            field=models.DateTimeField(blank=True, null=True,
                                       verbose_name="Остання успішна операція"),
        ),
        migrations.AddField(
            model_name="telegramaccount", name="last_error",
            field=models.TextField(blank=True, verbose_name="Остання помилка"),
        ),
        migrations.AddField(
            model_name="telegramaccount", name="gateway_connected",
            field=models.BooleanField(
                default=False, help_text="Gateway зараз тримає живий клієнт цього акаунта.",
                verbose_name="Зʼєднання в gateway"),
        ),
        migrations.RunPython(_deauth_state, migrations.RunPython.noop),
    ]
