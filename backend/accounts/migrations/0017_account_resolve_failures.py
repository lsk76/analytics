from django.db import migrations, models


class Migration(migrations.Migration):
    """Адаптивна пауза резолву: лічильник відмов поспіль (docs/tg-gateway-plan.md §4)."""

    dependencies = [("accounts", "0016_account_state")]

    operations = [
        migrations.AddField(
            model_name="telegramaccount", name="resolve_failures",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Кожна наступна відмова подовжує паузу резолву (6 → 12 → 24 год); "
                          "успішний резолв скидає.",
                verbose_name="Відмов резолву поспіль"),
        ),
    ]
