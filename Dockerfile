FROM python:3.12-slim

# Утилиты, которыми Claude будет пользоваться внутри контейнера.
# Остальное можно доустановить командой apt-get install прямо из чата.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl wget git nano less procps psmisc htop \
        iproute2 iputils-ping jq ripgrep tree unzip zip xz-utils \
        openssh-client rsync sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ .

ENV WORKDIR=/workspace \
    STATE_DIR=/data \
    PORT=8000 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /workspace /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS "http://127.0.0.1:$PORT/health" || exit 1
CMD ["python", "main.py"]
