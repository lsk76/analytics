#!/usr/bin/env bash
# ============================================================================
# backup.sh — pg_dump бази у backups/ з ротацією. Для cron на сервері аналітики.
#
# Ручний запуск:   bash deploy/backup.sh
# Cron (щодня 03:30, лог у backups/backup.log):
#   30 3 * * * cd /opt/tg-event-analytics && bash deploy/backup.sh >> backups/backup.log 2>&1
#
# Тримає останні $KEEP дампів (за замовч. 14). Формат -Fc (custom, стиснутий),
# відновлення: make restore FILE=backups/<name>.dump
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."          # корінь репозиторію
KEEP="${KEEP:-14}"
# Дампи БД містять секрети (Telethon session_string = повний доступ до TG-акаунта,
# паролі тощо) → створюємо їх 600, каталог 700. Без цього — world-readable.
umask 077
mkdir -p backups && chmod 700 backups

# Знаходимо запущений db-контейнер цього проєкту НЕЗАЛЕЖНО від стека (prod/monitor
# ділять одну назву проєкту й сервіс db), щоб cron працював за будь-якого з них.
DB_CONTAINER="$(docker ps \
  --filter label=com.docker.compose.project=tg-event-analytics \
  --filter label=com.docker.compose.service=db \
  --format '{{.Names}}' | head -1)"
[[ -z "$DB_CONTAINER" ]] && { echo "db-контейнер не знайдено (стек піднятий?)"; exit 1; }

TS="$(date +%Y%m%d_%H%M%S)"
OUT="backups/${TS}.dump"

# читаємо креденшли з .env (POSTGRES_USER/DB); дефолти як у compose
set -a; [[ -f .env ]] && . ./.env; set +a
PGUSER="${POSTGRES_USER:-postgres}"
PGDB="${POSTGRES_DB:-tg_events}"

echo "$(date '+%F %T') backup → ${OUT} (з $DB_CONTAINER)"
docker exec -i "$DB_CONTAINER" pg_dump -U "$PGUSER" -d "$PGDB" -Fc > "$OUT"
echo "  розмір: $(du -h "$OUT" | cut -f1)"

# ротація: лишаємо останні $KEEP
ls -1t backups/*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "  видаляю старий: $old"
    rm -f "$old"
done
