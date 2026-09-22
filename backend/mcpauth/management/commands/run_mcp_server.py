"""Мережевий MCP-сервер: ті самі інструменти, але через HTTP і з OAuth.

Відмінність від локального `mcp_server/server.py` — не в інструментах, а в тому,
ЯК до них дістаються:

    ноутбук:  Claude → stdio → server.py → docker compose exec → mcp_rpc → mcp_api
    прод:     клієнт → HTTPS → nginx → ЦЕЙ процес (у web-образі) → mcp_api

Тут немає ні docker-сокета, ні підпроцесів: інструменти викликаються в тому ж
процесі, що й Django. Тому docker-інструменти (`service_ps`, `service_restart`,
`worker_once`) тут відсутні за побудовою — вони лишаються локальними.

Кожен виклик іде від ІМЕНІ користувача з токена: видимість даних така сама, як
в адмінці, а роль вирішує, чи можна щось змінювати.

    python manage.py run_mcp_server --host 0.0.0.0 --port 8765
"""
import asyncio

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand

PY_TYPES = {"str": str, "int": int, "float": float, "bool": bool}


class Command(BaseCommand):
    help = "Підняти мережевий MCP-сервер (streamable-http + OAuth)"

    def add_arguments(self, parser):
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8765)
        parser.add_argument("--public-url", default="",
                            help="Публічна адреса (issuer). Типово MCP_PUBLIC_URL.")

    def handle(self, *args, **opts):
        from mcp.server.auth.settings import (AuthSettings, ClientRegistrationOptions,
                                              RevocationOptions)
        from mcp.server.mcpserver import MCPServer

        from analysis.services import mcp_api
        from mcpauth.oauth import DjangoOAuthProvider

        public = (opts["public_url"] or getattr(settings, "MCP_PUBLIC_URL", "")).rstrip("/")
        if not public:
            self.stderr.write("MCP_PUBLIC_URL не заданий — OAuth-метадані будуть хибні")
            return

        server = MCPServer(
            name="tg-analytics",
            instructions=(
                'Керування сервісом аналітики Telegram: акаунти й проксі, моніторинги '
                '(задачі/чати/джерела), збори, черги конвеєрів, TeleZip, публікація.\n\n'
                'Починай зі `service_health` — він показує, що стоїть і хто це розгрібає. '
                'Що саме тобі дозволено, видно в `tools_manifest`: у кожного інструмента '
                'вказано потрібний scope (mcp:read / mcp:write / mcp:admin).\n\n'
                'TeleZip — чотири інструменти: `tz_find` (пошук повідомлень; `stats=true` '
                'віддає лічильники без викачування), `tz_channels` (пошук каналів), '
                '`tz_users` (профілі людей) і безкоштовний `tz_status` (мережа, глибина '
                'індексу, слоти) — з нього варто починати перед збором.\n'
                'УВАГА, ЦІНА: один виклик до TeleZip ≈ $0.10 незалежно від обсягу, тож '
                '`limit=10000` коштує стільки ж, скільки `limit=10`. Обсяг питай '
                '`tz_find(stats=true)` (1 виклик), а не пошуком навмання; кожна наступна '
                'сторінка (`page_token`) і кожен чанк збору оплачуються окремо.\n'
                'УВАГА, СИНТАКСИС: діалекти протилежні. У `tz_find`/`tz_channels` (API v4) '
                'ПРОБІЛ = І, а АБО — це `|`: «(мигрант | приезж*) (драка | избил)». '
                'У запиті задачі (`task.telezip_query`, збір через `run_create`, API v3) '
                'навпаки: пробіл = АБО, а І — це `+`. Запит у чужому діалекті не падає з '
                'помилкою, а тихо віддає 0 збігів — і все одно коштує $0.10.\n\n'
                'Інструменти з поміткою «ЗМІНЮЄ СТАН» пишуть у БД те, що впливає на роботу '
                'сервісу. Службові позначки (як «востаннє використано» в `account_check`) '
                'такої помітки не дають — критерій саме в наслідках, а не в самому записі.'
                "\n\nВидимість даних — як в адмінці: свої задачі та свої або спільні "
                "акаунти. Зміни доступні за роллю, кожен виклик потрапляє в журнал.\n"
                "Docker-інструментів (`service_ps`, `service_logs`, `service_restart`, "
                "`worker_once`) тут НЕМА за побудовою: сокет у процес не прокинутий. "
                "Рестарт воркерів і читання логів контейнерів — локальна операція "
                "власника, тож це не «інструмент зник», а межа режиму: проси власника."),
            auth_server_provider=DjangoOAuthProvider(),
            auth=AuthSettings(
                issuer_url=public,
                resource_server_url=public,
                required_scopes=["mcp:read"],
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=["mcp:read", "mcp:write", "mcp:create", "mcp:admin"],
                    default_scopes=["mcp:read"]),
                revocation_options=RevocationOptions(enabled=True),
                # AS і resource server — той самий процес, а токени непрозорі й
                # звіряються з нашою ж таблицею, тож підміни аудиторії бути не може
                validate_token_resource=False),
        )

        n = self._register(server, mcp_api)
        self.stdout.write(f"[mcp] {n} інструментів; issuer {public}; "
                          f"слухаю {opts['host']}:{opts['port']}")
        # host/port — аргументи транспорту, а не поля Settings (у 2.x їх там нема)
        server.run("streamable-http", host=opts["host"], port=opts["port"])

    # ------------------------------------------------------------------ тули
    def _register(self, server, mcp_api) -> int:
        specs = mcp_api.manifest()
        for spec in specs:
            server.add_tool(self._build(spec, mcp_api), name=spec["name"],
                            description=self._describe(spec))
        return len(specs)

    @staticmethod
    def _describe(spec) -> str:
        need = {"mcp:read": "", "mcp:write": "ЗМІНЮЄ СТАН (роль оператора). ",
                "mcp:admin": "ЗМІНЮЄ СТАН (лише адмін). "}.get(spec["scope"], "")
        return need + spec["doc"]

    def _build(self, spec, mcp_api):
        """Синтезувати async-функцію з сигнатурою інструмента.

        Async обов'язково: SDK викликає тули в циклі подій, а всередині —
        синхронний Django ORM, тож робота йде в потоці через sync_to_async.
        """
        args = []
        for p in spec["params"]:
            ann = p["type"] if p["type"] in PY_TYPES else "str"
            if p.get("doc"):
                ann = f"Annotated[{ann}, Field(description={p['doc']!r})]"
            if p["required"]:
                args.append(f"{p['name']}: {ann}")
            elif p["default"] is None:
                args.append(f"{p['name']}: {ann} | None = None")
            else:
                args.append(f"{p['name']}: {ann} = {p['default']!r}")
        src = (f"async def {spec['name']}({', '.join(args)}) -> str:\n"
               f"    return await _run({spec['name']!r}, dict(locals()))\n")
        from pydantic import Field
        from typing import Annotated
        ns = {"_run": _make_runner(mcp_api), "Annotated": Annotated, "Field": Field}
        exec(src, ns)  # noqa: S102 — джерело будуємо з власного маніфесту
        return ns[spec["name"]]


def _make_runner(mcp_api):
    async def run(name: str, payload: dict) -> str:
        who = await sync_to_async(_actor_from_token, thread_sensitive=False)()
        clean = {k: v for k, v in payload.items() if v is not None}
        try:
            return await sync_to_async(mcp_api.call, thread_sensitive=False)(
                name, clean, who)
        except mcp_api.ToolError as e:
            return f"⚠ {e}"
    return run


def _actor_from_token():
    """Викликач із OAuth-токена: Django-юзер + скоупи. Без токена — відмова."""
    from django.contrib.auth.models import User
    from mcp.server.auth.middleware.auth_context import get_access_token

    from analysis.services.mcp_api import Actor
    from mcpauth.models import McpClient, McpRole

    token = get_access_token()
    if not token or not token.subject:
        raise PermissionError("немає дійсного токена")
    user = User.objects.filter(username=token.subject, is_active=True).first()
    if not user:
        raise PermissionError(f"користувача {token.subject} більше немає")
    role = McpRole.objects.filter(user=user, is_active=True).first()
    if not role:
        raise PermissionError(f"у {user.username} немає активної ролі MCP")
    client = McpClient.objects.filter(client_id=token.client_id).first()
    return Actor(user=user, scopes=list(token.scopes), role=role.role, client=client)
