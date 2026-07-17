# Generated for publish pipeline (PublishConfig + PublishedEvent).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('analysis', '0057_setting'),
    ]

    operations = [
        migrations.CreateModel(
            name='PublishConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120, verbose_name='Назва профілю')),
                ('is_active', models.BooleanField(default=False, verbose_name='Активний')),
                ('review_status', models.CharField(choices=[('pending', 'Очікує аудиту'), ('approved', 'Схвалено'), ('rejected', 'Відхилено (буде видалено)')], default='approved', help_text='Публікуємо лише події цього статусу.', max_length=12, verbose_name='Статус аудиту')),
                ('publish_from', models.DateField(blank=True, help_text='Беруться лише події з event_date >= цієї дати. Порожньо = без нижньої межі (УВАГА: перший прохід забере ВЕСЬ історичний беклог). Став дату активації, щоб публікувати лише нові події.', null=True, verbose_name='Публікувати події від (дата)')),
                ('chat_id', models.CharField(help_text='@username каналу або числовий id (напр. -1001234567890). Бот має бути адміном каналу.', max_length=64, verbose_name='Chat ID каналу')),
                ('bot_token', models.CharField(blank=True, help_text='Порожньо = береться з env TELEGRAM_BOT_TOKEN.', max_length=128, verbose_name='Bot token')),
                ('ai_model', models.CharField(blank=True, help_text='Порожньо = дефолтна LLM_MODEL.', max_length=120, verbose_name='AI-модель')),
                ('ai_prompt', models.TextField(blank=True, help_text='Системний промпт. Порожньо = дефолт із коду (services/publish/prompts.py). Модель має повертати JSON {publish: bool, reason, post_text}.', verbose_name='AI-промпт (фільтр+рерайт)')),
                ('max_per_pass', models.PositiveIntegerField(default=5, help_text='Скільки подій обробляти за один тік воркера (стримує вивал беклогу).', verbose_name='Макс. постів за прохід')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Створено')),
                ('region_subject', models.ForeignKey(blank=True, help_text='Порожньо = усі регіони.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='publish_configs', to='analysis.region', verbose_name="Суб'єкт РФ")),
                ('task', models.ForeignKey(blank=True, help_text='Порожньо = події всіх задач.', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='publish_configs', to='analysis.analysistask', verbose_name='Задача (збір)')),
                ('tags', models.ManyToManyField(blank=True, help_text='Порожньо = будь-які теги; інакше подія має мати ХОЧА Б ОДИН із цих тегів.', related_name='publish_configs', to='analysis.tag', verbose_name='Теги')),
            ],
            options={
                'verbose_name': 'Профіль публікації',
                'verbose_name_plural': 'Профілі публікації',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='PublishedEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('pending', 'В обробці'), ('skipped', 'Відсіяно AI'), ('published', 'Опубліковано'), ('failed', 'Помилка')], db_index=True, default='pending', max_length=12, verbose_name='Статус')),
                ('ai_verdict', models.BooleanField(blank=True, null=True, verbose_name='AI: публікувати')),
                ('ai_reason', models.TextField(blank=True, verbose_name='AI: причина')),
                ('post_text', models.TextField(blank=True, verbose_name='Текст поста (рерайт AI)')),
                ('tg_message_id', models.BigIntegerField(blank=True, null=True, verbose_name='TG message id')),
                ('published_at', models.DateTimeField(blank=True, null=True, verbose_name='Опубліковано о')),
                ('attempts', models.PositiveIntegerField(default=0, verbose_name='Спроби')),
                ('locked_at', models.DateTimeField(blank=True, null=True, verbose_name='Заблоковано о')),
                ('error', models.TextField(blank=True, verbose_name='Помилка')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Створено')),
                ('config', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='published', to='analysis.publishconfig', verbose_name='Профіль')),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='publications', to='analysis.event', verbose_name='Подія')),
            ],
            options={
                'verbose_name': 'Публікація події',
                'verbose_name_plural': 'Публікації подій',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='publishedevent',
            constraint=models.UniqueConstraint(fields=('config', 'event'), name='uniq_publish_config_event'),
        ),
        migrations.AddIndex(
            model_name='publishedevent',
            index=models.Index(fields=['config', 'status'], name='analysis_pu_config__d43f05_idx'),
        ),
    ]
