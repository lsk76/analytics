# Довідник, крок 2: назва/посилання/регіон/мова джерела живуть у рядку
# довідника (Channel, 1:1). Поля Source прибираються, channel стає обовʼязковим.
# ПЕРЕД цією міграцією на проді: manage.py directory_backfill (0084 + бекфіл
# звʼязує кожне джерело з довідником); перевірка нижче не пустить далі, якщо
# хоч одне джерело лишилося без рядка довідника.
from django.db import migrations, models
import django.db.models.deletion


def _check_all_linked(apps, schema_editor):
    Source = apps.get_model("analysis", "Source")
    n = Source.objects.filter(channel__isnull=True).count()
    if n:
        raise RuntimeError(
            f"{n} джерел без рядка довідника — спершу `manage.py directory_backfill`")


class Migration(migrations.Migration):

    dependencies = [
        ("analysis", "0084_directory_channel_url_source_channel"),
    ]

    operations = [
        migrations.RunPython(_check_all_linked, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="source", name="uniq_source_kind_url"),
        migrations.RemoveField(model_name="source", name="name"),
        migrations.RemoveField(model_name="source", name="url"),
        migrations.RemoveField(model_name="source", name="region_subject"),
        migrations.RemoveField(model_name="source", name="language"),
        migrations.AlterField(
            model_name="source",
            name="channel",
            field=models.OneToOneField(
                help_text="Довідник каналів і джерел (1:1): назва, посилання, регіон, підписники, мова живуть там.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="source", to="analysis.channel", verbose_name="Рядок довідника"),
        ),
        migrations.AlterModelOptions(
            name="source",
            options={"ordering": ["kind", "channel__title"],
                     "verbose_name": "Джерело (інформпростір)",
                     "verbose_name_plural": "Джерела (інформпростір)"},
        ),
        migrations.AlterModelOptions(
            name="sourcesubscription",
            options={"ordering": ["task", "priority", "source__channel__title"],
                     "verbose_name": "Підписка на джерело",
                     "verbose_name_plural": "Підписки на джерела"},
        ),
    ]
