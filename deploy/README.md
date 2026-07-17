# Деплой сервера аналітики (Ubuntu + nginx/certbot)

Покроковий runbook: захардити сервер і підняти повний аналітичний стек
(`docker-compose.prod.yml`: Postgres + gunicorn + важкі воркери
collect→enrich→precluster→classify→dedup→review) з адмінкою за HTTPS.

**Модель безпеки:** назовні відкриті лише `22/80/443`. gunicorn (`127.0.0.1:8001`)
і Postgres — тільки на loopback, у публічний інтернет не світяться. TLS термінує
nginx; Django в prod форсить secure-cookies + HSTS. Django-адмін доступний з
інтернету за логіном/паролем — тому пароль адміна мусить бути сильний
(валідатори увімкнено), а `/admin/login/` під nginx rate-limit.

---

## 0. Перед початком (з локальної машини)
```bash
# 1. Домен analytics.matter-d.pro → A-запис на IP сервера (перевір: dig +short analytics.matter-d.pro)
# 2. Закинь свій SSH-ключ на сервер (інакше хардинг замкне тебе):
ssh-copy-id youruser@<server-ip>
ssh youruser@<server-ip>          # переконайся, що заходиш БЕЗ пароля
```

## 1. Хардинг сервера
```bash
# на сервері, від імені свого юзера:
git clone <repo-url> /opt/tg-event-analytics
cd /opt/tg-event-analytics
ADMIN_USER=youruser sudo -E bash deploy/harden.sh
```
Робить: оновлення + автооновлення безпеки, UFW (лише 22/80/443), fail2ban,
SSH лише по ключах без root, Docker Engine + compose.

> ⚠️ **З ОКРЕМОГО вікна** перевір, що новий SSH-логін ключем працює, перш ніж
> закривати поточну сесію. Перелогінься, щоб застосувалась група `docker`.

## 2. Секрети (.env)
```bash
cp deploy/.env.prod.example .env
chmod 600 .env
nano .env      # заповни SECRET_KEY, POSTGRES_PASSWORD, API-ключі.
               # ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS уже виставлені на
               # analytics.matter-d.pro — міняй лише якщо домен інший.
```
Генератори підказані в коментарях файлу (`secrets.token_urlsafe`, `openssl rand`).

## 3. Підняти стек
```bash
make prod-analytics          # build + up -d (docker-compose.prod.yml)
make prod-analytics-ps       # усі сервіси healthy?
make prod-analytics-logs     # web: collectstatic + migrate + gunicorn стартанув?
```
`web` на старті сам робить `collectstatic` і `migrate`. Gunicorn слухає
`127.0.0.1:8001` — ззовні поки недоступний (це нормально, далі nginx).

Створи адміна (пароль ≥12 символів, інакше валідатор відхилить):
```bash
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```
За потреби — сідинг довідників: `make seed` (варіант через prod-compose:
`docker compose -f docker-compose.prod.yml exec web python manage.py seed_regions …`).

## 4. nginx + TLS
Використовуємо **офіційний nginx.org stable** (свіжа гілка ≥1.30; конфіг має
`http2 on;`, який дистрибутивний nginx 1.18 з Ubuntu не розуміє). Розкладка
nginx.org — `conf.d/`, а не `sites-enabled/`.

> ⚠️ **Порядок важливий:** спершу сертифікат, потім наш конфіг. Інакше `:443`
> посилатиметься на неіснуючий cert і nginx не стартує.

```bash
# --- репозиторій nginx.org ---
curl -fsSL https://nginx.org/keys/nginx_signing.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/nginx-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] https://nginx.org/packages/ubuntu jammy nginx" \
  | sudo tee /etc/apt/sources.list.d/nginx.list
printf 'Package: *\nPin: origin nginx.org\nPin-Priority: 900\n' | sudo tee /etc/apt/preferences.d/99nginx
sudo apt-get update
sudo apt-get install -y nginx certbot python3-certbot-nginx

# 1) Сертифікат — certbot тимчасово використає дефолтний :80-сайт для ACME.
sudo certbot certonly --nginx -d analytics.matter-d.pro

# 2) Тепер cert існує → кладемо наш конфіг у conf.d.
sudo cp deploy/nginx/tg-analytics.conf /etc/nginx/conf.d/tg-analytics.conf
sudo rm -f /etc/nginx/conf.d/default.conf
sudo nginx -t && sudo systemctl reload nginx
```
certbot сам ставить таймер автопродовження (`systemctl list-timers | grep certbot`).
Перевір: `https://analytics.matter-d.pro/admin/`.

## 5. Бекапи (cron)
```bash
crontab -e
# щодня 03:30, ротація 14 останніх дампів:
30 3 * * * cd /opt/tg-event-analytics && bash deploy/backup.sh >> backups/backup.log 2>&1
```
Відновлення: `make restore FILE=backups/<name>.dump`
(через prod-compose — див. `Makefile`, ціль використовує dev `docker compose`;
для prod: `docker compose -f docker-compose.prod.yml exec -T db pg_restore …`).

---

## Чеклист безпеки (пройдись після деплою)
- [ ] `sudo ufw status` — LISTEN лише 22/80/443; **немає** 8001, 5433 назовні
- [ ] `ss -tlnp | grep -E '8001|5433'` — слухають лише на `127.0.0.1`
- [ ] `curl -I http://analytics.matter-d.pro` → 301 на https
- [ ] `https://analytics.matter-d.pro/admin/` відкривається, сертифікат валідний
- [ ] SSH: `sudo sshd -T | grep -E 'permitrootlogin|passwordauth'` → `no`
- [ ] пароль суперюзера сильний; звичайних юзерів в адмінці зайвих немає
- [ ] `.env` має права `600`, не в git (`git status` чистий)
- [ ] `fail2ban-client status sshd` — активний
- [ ] перший `deploy/backup.sh` відпрацював, дамп ненульового розміру

## Оновлення коду згодом
```bash
cd /opt/tg-event-analytics && git pull
make prod-analytics-build            # rebuild + up (migrate виконається на старті web)
```

## Що НЕ робити
- Не відкривай 8001/5433 у UFW і не міняй bind у compose на `0.0.0.0`.
- Не став `DJANGO_DEBUG=true` на цьому сервері.
- Не тримай `ALLOWED_HOSTS=*` — лише свій домен.
- Перед схемними змінами БД — спершу `deploy/backup.sh` (CLAUDE.md-інваріант).
