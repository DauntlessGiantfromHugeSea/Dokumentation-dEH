FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    DB_PATH=/data/app.db \
    TZ=Europe/Berlin

# tzdata installieren damit Container-Zeit korrekt auf Europe/Berlin steht
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# timeout 300: PDF-Rendering (Akte mit Fotos), Sammel-Exporte und
#   frodor-Uploads dauern länger als die 30s-Voreinstellung — sonst
#   killt Gunicorn den Worker und der Browser zeigt einen 500er, ohne
#   dass die App eine Exception sieht.
# error-logfile auf dem Volume: überlebt Neustarts und ist über
#   /healthz einsehbar (wichtig ohne Shell-Zugriff auf den Server).
CMD ["sh", "-c", "gunicorn -w 2 --timeout 300 --graceful-timeout 30 \
    --error-logfile /data/gunicorn-error.log --capture-output --log-level info \
    -b 0.0.0.0:${PORT} app:app"]
