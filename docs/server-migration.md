# Переїзд на новий сервер — простий план

Простій не критичний: задачі, що йшли, доженуть пізніше самі (у infospace
watermark рухається лише по реально забраних елементах, тож дірки не буде —
буде затримка).

Злиття з локальною базою тут **не робимо** — прод їде як є. Злиття окремо, коли
новий сервер уже стоятиме (див. `docs/db-merge-analysis.md`).

Старий: `151.245.230.154`, користувач `deploy-analytics`, `/opt/tg-event-analytics`.

---

## 1. Новий сервер: база

Розмір: **8 GB / 4 ядра / 100 GB** (зараз усе займає 22 GB, з них додаток 7.6).

```bash
ssh-copy-id deploy-analytics@<НОВИЙ_IP>
ssh deploy-analytics@<НОВИЙ_IP>
git clone <repo-url> /opt/tg-event-analytics
cd /opt/tg-event-analytics
ADMIN_USER=deploy-analytics sudo -E bash deploy/harden.sh
```

`harden.sh` робить оновлення, UFW (лише 22/80/443), fail2ban, SSH лише по ключах,
Docker. Після нього **перелогінитись**, щоб застосувалася група `docker`.

## 2. Перенести секрети й сертифікат

```bash
# з локальної машини
scp tg-analytics:/opt/tg-event-analytics/.env /tmp/prod.env
scp /tmp/prod.env deploy-analytics@<НОВИЙ_IP>:/opt/tg-event-analytics/.env
ssh deploy-analytics@<НОВИЙ_IP> 'chmod 600 /opt/tg-event-analytics/.env'
rm /tmp/prod.env

# сертифікат: копіюємо, НЕ випускаємо заново — certbot по HTTP-01 не зможе,
# поки домен ще дивиться на старий сервер
ssh tg-analytics 'sudo tar czf /tmp/le.tgz -C /etc letsencrypt && sudo chown deploy-analytics /tmp/le.tgz'
scp tg-analytics:/tmp/le.tgz /tmp/ && scp /tmp/le.tgz deploy-analytics@<НОВИЙ_IP>:/tmp/
ssh deploy-analytics@<НОВИЙ_IP> 'sudo tar xzf /tmp/le.tgz -C /etc'
```

Плюс конфіг nginx із `deploy/nginx/`.

## 3. Зупинити старий і зняти дамп

```bash
ssh tg-analytics
cd /opt/tg-event-analytics
make prod-analytics-stop                      # або: docker compose -f docker-compose.prod.yml stop
docker compose -f docker-compose.prod.yml up -d db     # база потрібна для дампа
docker compose exec -T db sh -lc 'pg_dump -U $POSTGRES_USER $POSTGRES_DB' | gzip > /tmp/final.sql.gz
ls -lh /tmp/final.sql.gz                      # має бути ~600 MB
```

## 4. Перелити на новий

```bash
scp tg-analytics:/tmp/final.sql.gz /tmp/ && scp /tmp/final.sql.gz deploy-analytics@<НОВИЙ_IP>:/tmp/

ssh deploy-analytics@<НОВИЙ_IP>
cd /opt/tg-event-analytics
docker compose -f docker-compose.prod.yml up -d db
sleep 15
gunzip -c /tmp/final.sql.gz | docker compose exec -T db sh -lc 'psql -U $POSTGRES_USER -d $POSTGRES_DB'
make prod-analytics                           # підняти весь стек
```

## 5. Перевірити (5 хвилин)

```bash
docker compose -f docker-compose.prod.yml ps                    # усі Up
docker compose exec -T db sh -lc 'psql -U $POSTGRES_USER -d $POSTGRES_DB -tc "
  select (select count(*) from analysis_event), (select count(*) from analysis_post);"'
# має бути 128703 і 515303 — як на старому
curl -sI http://127.0.0.1:8001/admin/login/ | head -1     # 200
```

## 6. Cron

```bash
crontab -e
```
Два рядки зі старого:
```
30 3 * * * cd /opt/tg-event-analytics && bash deploy/backup.sh >> backups/backup.log 2>&1
*/10 * * * * cd /opt/tg-event-analytics && bash deploy/watchdog-collect.sh >> backups/watchdog.log 2>&1
```

## 7. DNS

A-запис `analytics.matter-d.pro` → новий IP.
TTL зараз 1799 с, тож повне перемикання займе до пів години.

```bash
dig +short analytics.matter-d.pro     # перевірити, що віддає новий IP
```

## 8. Старий сервер

**Не вимикати тиждень.** Це і є відкат: якщо щось не так — DNS назад на
`151.245.230.154`, і все працює як раніше.

---

## Потім, окремо

- злиття з локальною базою — `docs/db-merge-analysis.md`;
- Node + Claude Code + агент-ранер — `docs/agent-runner-plan.md`;
- накотити міграції 0063–0067 і залити довідник каналів (зараз лише локально).

Попередити пʼятьох користувачів адмінки про вікно.
