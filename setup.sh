#!/usr/bin/env bash
# Настраивает .env: домен, пароль, токен и способ HTTPS (встроенный Caddy или ваш nginx).
# Можно запускать повторно: пароль и токен сохраняются, заново выбирается только способ HTTPS.
set -euo pipefail
cd "$(dirname "$0")"

gen() {
    local s
    s=$(head -c 96 /dev/urandom | base64 | tr -d '/+=\n')
    echo "${s:0:$1}"
}

env_get() {
    grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true
}

# Заменяет или добавляет NAME=VALUE в .env, сохраняя права файла.
env_set() {
    local tmp
    tmp=$(mktemp)
    grep -vE "^$1=" .env > "$tmp" || true
    echo "$1=$2" >> "$tmp"
    cat "$tmp" > .env
    rm -f "$tmp"
}

port_busy() {
    if command -v ss >/dev/null 2>&1; then
        [[ -n "$(ss -ltnH "sport = :$1" 2>/dev/null)" ]]
    else
        (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
    fi
}

# Кто держит порт: docker:<контейнер>, имя веб-сервера или unknown.
port_holder() {
    local c p
    c=$(docker ps --filter "publish=$1" --format '{{.Names}}' 2>/dev/null | head -1 || true)
    if [[ -n "$c" ]]; then
        echo "docker:$c"
        return
    fi
    for p in nginx apache2 httpd caddy traefik haproxy lighttpd; do
        if pgrep -x "$p" >/dev/null 2>&1; then
            echo "$p"
            return
        fi
    done
    echo unknown
}

# ---- домен и секреты ----------------------------------------------------------

if [[ -f .env ]]; then
    DOMAIN=$(env_get DOMAIN)
    echo "Найден .env (домен: $DOMAIN). Пароль и токен оставляю прежними."
else
    read -rp "Домен сервера (например mcp.example.com): " DOMAIN
    if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
        echo "Некорректный домен: '$DOMAIN'"
        exit 1
    fi
    (
        umask 077
        cat > .env <<EOF
DOMAIN=$DOMAIN
ADMIN_PASSWORD=$(gen 24)
API_TOKEN=$(gen 48)
EOF
    )
fi
mkdir -p workspace data

# ---- выбор способа HTTPS -------------------------------------------------------

# Контейнеры этого проекта сами занимают порты — останавливаем их перед проверкой.
if [[ -n "$(COMPOSE_PROFILES=caddy docker compose ps -aq 2>/dev/null || true)" ]]; then
    echo "Останавливаю контейнеры проекта, чтобы проверить порты..."
    COMPOSE_PROFILES=caddy docker compose down >/dev/null 2>&1 || true
fi

LOCAL_PORT=$(env_get MCP_LOCAL_PORT)
LOCAL_PORT=${LOCAL_PORT:-8000}
while port_busy "$LOCAL_PORT"; do
    LOCAL_PORT=$((LOCAL_PORT + 1))
done
env_set MCP_LOCAL_PORT "$LOCAL_PORT"

BUSY=""
port_busy 80 && BUSY="80"
port_busy 443 && BUSY="${BUSY:+$BUSY и }443"

if [[ -z "$BUSY" ]]; then
    env_set COMPOSE_PROFILES caddy
    cat <<EOF

Порты 80 и 443 свободны — HTTPS сделает встроенный Caddy.

  Запуск:          docker compose up -d --build
  URL коннектора:  https://$DOMAIN/mcp
  Пароль и токен:  cat .env
EOF
    exit 0
fi

env_set COMPOSE_PROFILES ""
sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PORT__/$LOCAL_PORT/g" nginx-site.conf.template > nginx-site.conf
HOLDER=$(port_holder 80)
[[ "$HOLDER" == unknown ]] && HOLDER=$(port_holder 443)

cat <<EOF

Порт $BUSY уже занят ($HOLDER), поэтому встроенный Caddy выключен.
MCP-сервер будет слушать только локально: http://127.0.0.1:$LOCAL_PORT

Сначала запустите контейнер:  docker compose up -d --build
Проверка:                     curl http://127.0.0.1:$LOCAL_PORT/health   (ответ: ok)

EOF

case "$HOLDER" in
    nginx)
        cat <<EOF
Порт занят nginx. Подключите MCP через него (готовый конфиг: nginx-site.conf):

  sudo cp nginx-site.conf /etc/nginx/sites-available/claude-mcp.conf
  sudo ln -sf /etc/nginx/sites-available/claude-mcp.conf /etc/nginx/sites-enabled/
  sudo nginx -t && sudo systemctl reload nginx
  sudo apt install -y certbot python3-certbot-nginx     # если certbot ещё нет
  sudo certbot --nginx -d $DOMAIN

Если на этом nginx нет нужных вам сайтов, можно просто выключить его:
  sudo systemctl disable --now nginx && ./setup.sh
EOF
        ;;
    apache2 | httpd)
        cat <<EOF
Порт занят Apache. Настройка прокси для Apache — в README, раздел «Порты 80/443 заняты».
Если на Apache нет нужных вам сайтов, можно просто выключить его:
  sudo systemctl disable --now $HOLDER && ./setup.sh
EOF
        ;;
    caddy)
        cat <<EOF
Порт занят Caddy, установленным на сервере. Добавьте в его Caddyfile:

  $DOMAIN {
      reverse_proxy 127.0.0.1:$LOCAL_PORT
  }

и выполните: sudo systemctl reload caddy
EOF
        ;;
    docker:*)
        cat <<EOF
Порт занят Docker-контейнером «${HOLDER#docker:}».
Если он не нужен:  docker stop ${HOLDER#docker:} && ./setup.sh
Если это ваш прокси для других сайтов — направьте в нём домен $DOMAIN
на 127.0.0.1:$LOCAL_PORT (пример для nginx — в файле nginx-site.conf).
EOF
        ;;
    *)
        cat <<EOF
Узнать, какая программа занимает порт:  sudo ss -ltnp 'sport = :80'
Если она не нужна — остановите её и запустите ./setup.sh ещё раз.
Если это ваш веб-сервер — направьте домен $DOMAIN на 127.0.0.1:$LOCAL_PORT
(готовый пример для nginx: nginx-site.conf, остальное — в README).
EOF
        ;;
esac
