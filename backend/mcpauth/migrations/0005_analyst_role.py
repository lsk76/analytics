from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mcpauth', '0004_telezip_quota'),
    ]

    operations = [
        migrations.AlterField(
            model_name='mcprole',
            name='role',
            field=models.CharField(
                choices=[('reader', 'Читач — лише перегляд стану'),
                         ('operator', 'Оператор — збори, чати, джерела, акаунти (наявні)'),
                         ('analyst', 'Просунутий аналітик — + створює дослідження, канали, джерела, акаунти'),
                         ('admin', 'Адмін — усе, включно з налаштуваннями й контейнерами')],
                default='reader', max_length=10, verbose_name='Роль'),
        ),
    ]
