"""Платні виклики TeleZip чекають згоди людини (`McpPendingCall`)."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mcpauth", "0006_role_to_admission"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="McpPendingCall",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                           verbose_name="ID")),
                ("tool", models.CharField(db_index=True, max_length=64, verbose_name="Інструмент")),
                ("payload", models.JSONField(blank=True, default=dict, verbose_name="Параметри")),
                ("payload_hash", models.CharField(db_index=True, max_length=64,
                                                  verbose_name="Хеш параметрів")),
                ("code", models.CharField(db_index=True, max_length=12,
                                          verbose_name="Код підтвердження")),
                ("requests", models.PositiveSmallIntegerField(default=1,
                                                             verbose_name="Платних запитів")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Створено")),
                ("used_at", models.DateTimeField(blank=True, null=True,
                                                 verbose_name="Використано")),
                ("user", models.ForeignKey(blank=True, null=True,
                                           on_delete=django.db.models.deletion.CASCADE,
                                           related_name="mcp_pending",
                                           to=settings.AUTH_USER_MODEL,
                                           verbose_name="Користувач")),
            ],
            options={
                "verbose_name": "Платний виклик на підтвердженні",
                "verbose_name_plural": "Платні виклики на підтвердженні",
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["code", "used_at"],
                                         name="mcpauth_mcp_code_5d1eae_idx")],
            },
        ),
    ]
