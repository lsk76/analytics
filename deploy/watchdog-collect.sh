#!/usr/bin/env bash
# ============================================================================
# watchdog-collect.sh — страховка від зависань info-collect.
#
# Якщо останній зібраний пост старший за MAX_AGE_MIN хвилин — рестартує
# worker-info-collect (однорепліковий колектор інколи блокується на підвислому
# fetch, який навіть таймаут може не покрити). Рестарт ідемпотентний: воркер
# продовжує з watermark.
#
# Cron (кожні 10 хв):
#   */10 * * * * cd /opt/tg-event-analytics && bash deploy/watchdog-collect.sh >> backups/watchdog.log 2>&1
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
MAX_AGE_MIN="${MAX_AGE_MIN:-30}"
DC="docker compose -f docker-compose.monitor.yml"

set -a; [[ -f .env ]] && . ./.env; set +a
PGUSER="${POSTGRES_USER:-tg_events}"
PGDB="${POSTGRES_DB:-tg_events}"

age=$($DC exec -T db psql -U "$PGUSER" -d "$PGDB" -tAc \
  "SELECT COALESCE(EXTRACT(EPOCH FROM (now()-max(created_at)))/60, 999999)::int FROM analysis_post;" \
  2>/dev/null | tr -d '[:space:]')

if [[ ! "$age" =~ ^[0-9]+$ ]]; then
  echo "$(date '+%F %T') watchdog: не вдалось прочитати вік поста ('$age') — пропуск"
  exit 0
fi

if (( age > MAX_AGE_MIN )); then
  echo "$(date '+%F %T') watchdog: останній пост ${age}хв тому (> ${MAX_AGE_MIN}) → РЕСТАРТ info-collect"
  $DC restart worker-info-collect
else
  echo "$(date '+%F %T') watchdog: ок (останній пост ${age}хв тому)"
fi
