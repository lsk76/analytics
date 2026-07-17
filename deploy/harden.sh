#!/usr/bin/env bash
# ============================================================================
# harden.sh — базовий хардинг Ubuntu/Debian сервера під tg-event-analytics.
#
# Робить: оновлення, автооновлення безпеки, UFW (лише SSH+HTTP+HTTPS),
# fail2ban, посилення SSH (ключі, без root-логіну), встановлення Docker.
#
# Запуск ОДИН РАЗ від root (або sudo), ПІСЛЯ того як ти додав свій SSH-ключ:
#     ssh-copy-id user@server        # з локальної машини, ДО запуску
#     sudo bash deploy/harden.sh
#
# ⚠️ Скрипт вимикає вхід по паролю в SSH. Переконайся, що ключ працює
#    (заходиш без пароля) — інакше залишишся замкненим ззовні.
# ============================================================================
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Запусти від root: sudo bash $0"; exit 1; fi

# --- дозволений юзер для SSH (не root). Передай як env або підставиться SUDO_USER ---
ADMIN_USER="${ADMIN_USER:-${SUDO_USER:-}}"
SSH_PORT="${SSH_PORT:-22}"

echo "==> [1/7] Оновлення системи"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

echo "==> [2/7] Автооновлення безпеки (unattended-upgrades)"
apt-get install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> [3/7] Базові пакети + fail2ban"
apt-get install -y ufw fail2ban curl ca-certificates gnupg

echo "==> [4/7] UFW: default deny in, дозволяємо лише SSH/HTTP/HTTPS"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp"        # SSH
ufw allow 80/tcp                   # HTTP (certbot renew + редирект на https)
ufw allow 443/tcp                  # HTTPS
# ⚠️ НЕ відкриваємо 8001 (gunicorn) і 5433 (postgres) — вони лише на loopback.
ufw --force enable
ufw status verbose

echo "==> [5/7] fail2ban: захист SSH від брутфорсу"
cat >/etc/fail2ban/jail.d/sshd.local <<EOF
[sshd]
enabled = true
port    = ${SSH_PORT}
maxretry = 5
bantime  = 1h
findtime = 10m
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

echo "==> [6/7] Посилення SSH"
if [[ -z "$ADMIN_USER" ]]; then
  echo "!! ADMIN_USER не визначено. Запусти: ADMIN_USER=youruser sudo -E bash $0"
  echo "!! SSH-конфіг НЕ чіпаю, щоб не замкнути тебе."
else
  # переконаймося, що у юзера є authorized_keys
  KEYFILE="/home/${ADMIN_USER}/.ssh/authorized_keys"
  if [[ ! -s "$KEYFILE" ]]; then
    echo "!! У ${ADMIN_USER} немає SSH-ключів ($KEYFILE порожній)."
    echo "!! Спершу з локальної машини: ssh-copy-id ${ADMIN_USER}@<server>"
    echo "!! Вхід по паролю НЕ вимикаю (щоб не замкнути)."
  else
    cat >/etc/ssh/sshd_config.d/99-hardening.conf <<EOF
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
X11Forwarding no
MaxAuthTries 3
AllowUsers ${ADMIN_USER}
Port ${SSH_PORT}
EOF
    sshd -t && systemctl reload ssh 2>/dev/null || systemctl reload sshd
    echo "   SSH: root-логін вимкнено, лише ключі, лише ${ADMIN_USER}."
  fi
fi

echo "==> [7/7] Docker Engine + compose plugin (офіційний репозиторій)"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
if [[ -n "$ADMIN_USER" ]]; then usermod -aG docker "$ADMIN_USER" || true; fi

echo
echo "============================================================"
echo "ГОТОВО. Перевір ПЕРЕД тим як закривати сесію:"
echo "  • новий SSH-логін ключем працює (окреме вікно!)"
echo "  • ufw status  — відкриті лише ${SSH_PORT}, 80, 443"
echo "  • docker ps   — доступний (перелогінься для docker-групи)"
echo "Далі: nginx+certbot і деплой (див. deploy/README.md)"
echo "============================================================"
