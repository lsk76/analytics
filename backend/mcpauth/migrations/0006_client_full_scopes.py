"""Наявним OAuth-клієнтам — повний набір скоупів у запиті.

Клієнти реєструвалися, поки `default_scopes` був `["mcp:read"]`, і при кожній
авторизації просили саме читання: оператор бачив на сторінці згоди лише
«mcp:read» і отримував токен без права щось змінити. Стеля й далі роль
(`policy.granted_scopes`), тож ширший запит нікому не додає прав — лише
перестає їх зрізати.
"""
from django.db import migrations

FULL = "mcp:read mcp:write mcp:create mcp:admin"


def widen(apps, schema_editor):
    apps.get_model("mcpauth", "McpClient").objects.filter(
        scope__in=["", "mcp:read"]).update(scope=FULL)


def narrow(apps, schema_editor):
    apps.get_model("mcpauth", "McpClient").objects.filter(scope=FULL).update(scope="mcp:read")


class Migration(migrations.Migration):
    dependencies = [("mcpauth", "0005_analyst_role")]
    operations = [migrations.RunPython(widen, narrow)]
