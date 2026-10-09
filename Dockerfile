FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared shared
COPY dispatch dispatch
COPY hub hub

# APP=dispatch|hub 決定啟動的應用程式；hub 的指標只在記憶體，必須單一 worker
CMD case "$APP" in dispatch|hub) ;; *) echo "APP must be dispatch or hub" >&2; exit 1;; esac; \
    exec uvicorn "$APP.main:app" --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
