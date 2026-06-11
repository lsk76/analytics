"""
Opinion-monitor pipeline: extend Post for comment-level data and add
MonitorChat to enroll chats into a monitoring AnalysisTask.

What changes:
  * Post.author_name / author_tg_id / reply_to_msg / also_in_chats / tags(M2M)
    — needed to treat each Post as a single user comment (vs. the existing
    aggregated-event model).
  * MonitorChat — DB-managed whitelist tying a Channel to an AnalysisTask;
    replaces the YAML-config approach. Editable in admin.

Migration is purely additive: existing Post rows get sensible defaults
(empty author, empty also_in_chats list, no tags), nothing about the
ethnic-clashes pipeline changes.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("analysis", "0020_region_population"),
    ]

    operations = [
        # --- Post: comment-level fields ----------------------------------
        migrations.AddField(
            model_name="post",
            name="author_name",
            field=models.CharField(
                blank=True, max_length=128,
                verbose_name="Автор (FromUserName)",
                help_text="Юзернейм або відображене ім'я автора коментаря",
            ),
        ),
        migrations.AddField(
            model_name="post",
            name="author_tg_id",
            field=models.BigIntegerField(
                null=True, blank=True, db_index=True,
                verbose_name="Автор tg_id",
                help_text="FromUserId з TeleZip — для дедупу один-автор-у-багатьох-чатах",
            ),
        ),
        migrations.AddField(
            model_name="post",
            name="reply_to_msg",
            field=models.BigIntegerField(
                null=True, blank=True,
                verbose_name="ReplyTo msg_id",
                help_text="Якщо це коментар-відповідь у linked-discussion",
            ),
        ),
        migrations.AddField(
            model_name="post",
            name="also_in_chats",
            field=models.JSONField(
                default=list, blank=True,
                verbose_name="Також у чатах",
                help_text=("Список username чатів, де той самий автор написав "
                           "ідентичний текст (збираємо як 1 Post замість N)."),
            ),
        ),
        migrations.AddField(
            model_name="post",
            name="tags",
            field=models.ManyToManyField(
                to="analysis.Tag", blank=True, related_name="posts",
                verbose_name="Теги",
                help_text=("Теги opinion/topic/criticism_target. "
                           "Заповнюються LLM-тегувальником."),
            ),
        ),
        # --- MonitorChat -------------------------------------------------
        migrations.CreateModel(
            name="MonitorChat",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name="ID")),
                ("is_active", models.BooleanField(
                    default=True, db_index=True, verbose_name="Активний",
                    help_text=("Зніми галочку щоб виключити чат з наступного збору, "
                               "не видаляючи історичні дані."),
                )),
                ("is_critical_source", models.BooleanField(
                    default=False, db_index=True, verbose_name="Критичне джерело",
                    help_text="Особливо важливий чат — пріоритет у звітах.",
                )),
                ("priority", models.PositiveSmallIntegerField(
                    default=100, verbose_name="Пріоритет",
                    help_text="Менше = вище у списку. Для сортування при показі.",
                )),
                ("notes", models.TextField(blank=True, verbose_name="Нотатки")),
                ("added_by", models.CharField(
                    max_length=80, blank=True, verbose_name="Хто додав",
                    help_text="Хто/коли додав чат у whitelist (ручний рядок).",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True,
                                                    verbose_name="Створено")),
                ("channel", models.ForeignKey(
                    to="analysis.Channel", on_delete=models.deletion.CASCADE,
                    related_name="enrolled_in", verbose_name="Чат",
                )),
                ("task", models.ForeignKey(
                    to="analysis.AnalysisTask", on_delete=models.deletion.CASCADE,
                    related_name="monitor_chats", verbose_name="Задача моніторингу",
                )),
            ],
            options={
                "verbose_name": "Чат моніторингу",
                "verbose_name_plural": "Чати моніторингу",
                "ordering": ["task", "priority", "channel__username"],
                "unique_together": {("task", "channel")},
                "indexes": [models.Index(fields=["task", "is_active"],
                                          name="analysis_mo_task_id_act_idx")],
            },
        ),
    ]
