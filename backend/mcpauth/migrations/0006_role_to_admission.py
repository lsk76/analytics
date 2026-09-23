"""Роль MCP більше не задається вручну: скоупи виводяться з прав Django
(`policy.scopes_for`), а рядок лишається допуском + необовʼязковою стелею +
квотою TeleZip.

Було два паралельні механізми на одне питання «що людині можна»: права
Django (розділи адмінки) і роль MCP (скоупи). Вони дублювалися — і
розходились: оператор міг мати в адмінці `add_source`, а роль не давала
`mcp:create`. Тепер джерело правди одне.

Перенос: reader → стеля «лише читання» (єдиний випадок, коли роль реально
звужувала права); operator/analyst/admin → без стелі (рівно те, що дає
адмінка; суперюзер і далі отримує все).
"""
from django.db import migrations, models


def role_to_cap(apps, schema_editor):
    apps.get_model("mcpauth", "McpRole").objects.filter(role="reader").update(max_scope="mcp:read")


def cap_to_role(apps, schema_editor):
    McpRole = apps.get_model("mcpauth", "McpRole")
    McpRole.objects.filter(max_scope="mcp:read").update(role="reader")
    McpRole.objects.exclude(max_scope="mcp:read").update(role="admin")


class Migration(migrations.Migration):
    dependencies = [("mcpauth", "0005_analyst_role")]

    operations = [
        migrations.AddField(
            model_name="mcprole",
            name="max_scope",
            field=models.CharField(
                blank=True, default="", max_length=12,
                choices=[("", "Як в адмінці (за правами Django)"),
                         ("mcp:read", "Лише читання"),
                         ("mcp:write", "Читання і зміни (без створення й адмінського)"),
                         ("mcp:create", "Читання, зміни, створення (без адмінського)")],
                help_text="Порожньо = рівно те, що людина може в адмінці. Інше значення "
                          "ЗВУЖУЄ доступ через MCP, розширити ним не можна.",
                verbose_name="Стеля доступу"),
        ),
        migrations.RunPython(role_to_cap, cap_to_role),
        migrations.RemoveField(model_name="mcprole", name="role"),
        migrations.AlterModelOptions(
            name="mcprole",
            options={"ordering": ["user__username"], "verbose_name": "Доступ до MCP",
                     "verbose_name_plural": "Доступи до MCP"},
        ),
    ]
