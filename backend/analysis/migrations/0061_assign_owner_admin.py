"""Призначити наявним задачам і профілям публікації owner = перший суперюзер.

Разова data-міграція: до появи поля owner усі рядки були «нічиї». Ставимо їх на
першого суперюзера (admin), щоб при повній ізоляції вони не «зникли» з адмінки
для всіх, крім суперюзера. Нові рядки owner отримують у save_model адмінки.
"""
from django.conf import settings
from django.db import migrations


def assign_owner(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    AnalysisTask = apps.get_model("analysis", "AnalysisTask")
    PublishConfig = apps.get_model("analysis", "PublishConfig")
    su = User.objects.filter(is_superuser=True).order_by("id").first()
    if su is None:
        return
    AnalysisTask.objects.filter(owner__isnull=True).update(owner=su)
    PublishConfig.objects.filter(owner__isnull=True).update(owner=su)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("analysis", "0060_analysistask_owner_publishconfig_owner"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.RunPython(assign_owner, noop)]
