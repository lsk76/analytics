"""
python manage.py run_tg_gateway [--host 0.0.0.0] [--port 8010]

Єдиний процес, де живе Telethon: 1 довгоживучий клієнт на акаунт + lock.
Споживачі ходять через accounts.services.managed.ManagedAccount (HTTP).
Див. docs/tg-gateway-plan.md.
"""
import asyncio
import logging
import os

from django.core.management.base import BaseCommand

logger = logging.getLogger("accounts.gateway")


class Command(BaseCommand):
    help = "Telegram gateway: усі Telethon-клієнти в одному процесі"

    def add_arguments(self, parser):
        parser.add_argument("--host", default=os.environ.get("TG_GATEWAY_HOST", "0.0.0.0"))
        parser.add_argument("--port", type=int,
                            default=int(os.environ.get("TG_GATEWAY_PORT", "8010")))

    def handle(self, *args, **opts):
        from accounts.gateway.server import serve
        logging.getLogger("telethon").setLevel(logging.WARNING)
        asyncio.run(serve(opts["host"], opts["port"]))
