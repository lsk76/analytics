from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mcpauth', '0003_mcpclient_client_secret_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='mcprole',
            name='telezip_daily_limit',
            field=models.PositiveIntegerField(
                default=0, help_text='0 = дефолт із налаштування mcp_telezip_daily_limit.',
                verbose_name='TeleZip: ліміт запитів/добу'),
        ),
        migrations.AddField(
            model_name='mcpauditlog',
            name='paid_requests',
            field=models.PositiveSmallIntegerField(
                default=0, verbose_name='Платних запитів TeleZip'),
        ),
    ]
