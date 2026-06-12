from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("analysis", "0021_post_opinion_fields_and_monitor_chat"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysistask",
            name="pipeline",
            field=models.CharField(
                choices=[
                    ("events", "Події (enrich→precluster→classify→dedup)"),
                    ("monitor", "Моніторинг думок (filter→prescreen→tag)"),
                ],
                db_index=True,
                default="events",
                help_text=(
                    "events-воркери та monitor-воркери беруть лише «свої» задачі — "
                    "пост задачі ніколи не потрапить у чужу стадію."
                ),
                max_length=12,
                verbose_name="Конвеєр",
            ),
        ),
        migrations.AlterField(
            model_name="post",
            name="stage",
            field=models.CharField(
                choices=[
                    ("collected", "Зібрано"),
                    ("enriched", "Збагачено"),
                    ("preclustered", "Прекластеризовано"),
                    ("classified", "Класифіковано"),
                    ("deduped", "Дедупльовано"),
                    ("mon_collected", "Монітор: зібрано"),
                    ("mon_filtered", "Монітор: відфільтровано"),
                    ("mon_prescreened", "Монітор: прескрін+"),
                    ("done", "Готово"),
                    ("failed", "Помилка"),
                ],
                db_index=True,
                default="collected",
                max_length=16,
                verbose_name="Стадія конвеєра",
            ),
        ),
    ]
