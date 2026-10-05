#!/bin/sh
# Stdio-обгортка MCP-сервера tgstat (docs/tgstat-service.md, розділ MCP).
# Сервер живе в контейнері tgstat на проді; сюди лише прокидаємо stdio.
#   TGSTAT_SSH   — хост із ~/.ssh/config (дефолт tg-analytics); local — без ssh
#                  (на самому сервері).
#   TGSTAT_DIR   — каталог прод-стеку (дефолт /opt/tg-event-analytics).
HOST="${TGSTAT_SSH:-tg-analytics}"
DIR="${TGSTAT_DIR:-/opt/tg-event-analytics}"
CMD="cd $DIR && exec docker compose -f docker-compose.prod.yml exec -T tgstat python -m app.mcp_server"
if [ "$HOST" = "local" ] || [ "$HOST" = "-" ]; then
  exec sh -c "$CMD"
fi
# -T: без tty (stdio MCP — чистий потік); BatchMode: не питати пароль у фоні.
exec ssh -T -o BatchMode=yes -o ServerAliveInterval=30 "$HOST" "$CMD"
