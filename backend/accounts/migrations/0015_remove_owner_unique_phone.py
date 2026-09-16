from django.db import migrations, models


class Migration(migrations.Migration):
    """Прибрати owner (злито в user). (user, phone) → унікальний phone: з nullable
    user пара більше не гарантує, що номер не заведено двічі."""

    dependencies = [("accounts", "0014_merge_owner_into_user")]

    operations = [
        migrations.RemoveField(model_name="telegramaccount", name="owner"),
        migrations.AlterUniqueTogether(name="telegramaccount", unique_together=set()),
        migrations.AlterField(
            model_name="telegramaccount",
            name="phone_number",
            field=models.CharField(help_text="Номер телефону з кодом країни (напр., +380501234567)", max_length=20, unique=True, verbose_name="Номер телефону"),
        ),
    ]
