from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("analysis", "0055_analysistask_info_max_age_days"),
    ]

    operations = [
        migrations.RenameField(
            model_name="source",
            old_name="state",
            new_name="poll_cursor",
        ),
        migrations.AlterField(
            model_name="source",
            name="poll_cursor",
            field=models.JSONField(
                blank=True, default=dict,
                help_text="Позиція «докуди вже зібрано» для цього джерела, щоб брати "
                          "лише нове: Telegram — last_msg_id; RSS — seen_ids+etag; "
                          "web — seen_ids. Очистити (→ порожньо) = перечитати заново "
                          "(backfill).",
                verbose_name="Курсор збору (докуди прочитано)"),
        ),
    ]
