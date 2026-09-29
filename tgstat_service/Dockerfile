# Окремий stateless-сервіс TGStat (docs/tgstat-service.md). Без Django і БД:
# Playwright + постійний headed Chrome на Xvfb, вхід руками через noVNC.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# Без noninteractive apt зависає на запитах debconf (розкладка клавіатури тощо).
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Google Chrome є лише під amd64 (прод); на arm64 сервіс бере вбудований Chromium.
RUN if [ "$(dpkg --print-architecture)" = "amd64" ]; then playwright install chrome; fi

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV TGSTAT_PROFILE=/data/profile PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8020 6080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.server"]
