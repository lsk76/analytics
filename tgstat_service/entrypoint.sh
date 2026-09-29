#!/bin/sh
# Віртуальний дисплей :99 для headed Chrome + (за наявності пароля) VNC/noVNC,
# через які людина один раз логіниться на tgstat у ЦЬОМУ Ж браузері.
set -e

W="${SCREEN_W:-1366}"; H="${SCREEN_H:-900}"
# /tmp переживає рестарт контейнера, а старий лок не дає Xvfb стартувати.
rm -f /tmp/.X11-unix/X99 /tmp/.X99-lock
mkdir -p /tmp/.X11-unix
Xvfb :99 -screen 0 "${W}x${H}x24" -ac >>/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
i=0; while [ ! -S /tmp/.X11-unix/X99 ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i+1)); done

if [ -n "$TGSTAT_VNC_PASSWORD" ]; then
  x11vnc -storepasswd "$TGSTAT_VNC_PASSWORD" /tmp/.vncpw >/dev/null 2>&1
  # Порти публікуються в compose лише на 127.0.0.1 — доступ тільки SSH-тунелем.
  ( while true; do
      x11vnc -display :99 -forever -shared -rfbauth /tmp/.vncpw \
        -rfbport 5900 -quiet >>/tmp/x11vnc.log 2>&1
      sleep 3
    done ) &
  ( while true; do
      websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 \
        >>/tmp/novnc.log 2>&1
      sleep 3
    done ) &
else
  echo "TGSTAT_VNC_PASSWORD порожній — VNC вимкнено, увійти на tgstat не вийде" >&2
fi

exec "$@"
