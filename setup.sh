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

env_del() {
    local tmp
    tmp=$(mktemp)
    grep -vE "^$1=" .env > "$tmp" || true
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

# Кто держит порт: docker:<контейнер>, имя процесса (ss видит его под root), веб-сервер или unknown.
port_holder() {
    local c p
    c=$(docker ps --filter "publish=$1" --format '{{.Names}}' 2>/dev/null | head -1 || true)
    if [[ -n "$c" ]]; then
        echo "docker:$c"
        return
    fi
    p=$(ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2 || true)
    if [[ -n "$p" ]]; then
        echo "$p"
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
    read -rp "Домен сервера (например mcp.example.com): " DOMAIN || true
    (
        umask 077
        echo "DOMAIN=$DOMAIN" > .env
    )
fi
if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z0-9-]{2,}$ ]]; then
    echo "Некорректный домен: '$DOMAIN' (исправьте DOMAIN в .env)"
    exit 1
fi
[[ -n "$(env_get ADMIN_PASSWORD)" ]] || env_set ADMIN_PASSWORD "$(gen 24)"
[[ -n "$(env_get API_TOKEN)" ]] || env_set API_TOKEN "$(gen 48)"
mkdir -p workspace data

# ---- выбор способа HTTPS -------------------------------------------------------

# Контейнеры этого проекта сами занимают порты — останавливаем их перед проверкой.
if [[ -n "$(COMPOSE_PROFILES=caddy,sni docker compose ps -aq 2>/dev/null || true)" ]]; then
    echo "Останавливаю контейнеры проекта, чтобы проверить порты..."
    COMPOSE_PROFILES=caddy,sni docker compose down >/dev/null 2>&1 || true
fi

LOCAL_PORT=$(env_get MCP_LOCAL_PORT)
LOCAL_PORT=${LOCAL_PORT:-8000}
while port_busy "$LOCAL_PORT"; do
    LOCAL_PORT=$((LOCAL_PORT + 1))
done
env_set MCP_LOCAL_PORT "$LOCAL_PORT"
env_del HTTPS_PORT   # от прошлой версии

# Порт 80 свободен — Caddy получает сертификат через него.
if ! port_busy 80; then
    # claude.ai подключается только к порту 443. Если 443 занят (например, VPN) — делим его
    # через HAProxy по имени домена. Режим «общий 443» сохраняется при повторных запусках.
    if [[ "$(env_get COMPOSE_PROFILES)" == *sni* ]] || port_busy 443; then
        VPN_PORT=$(env_get VPN_PORT)
        if [[ ! "$VPN_PORT" =~ ^[1-9][0-9]*$ ]]; then
            VPN_PORT=4443
            while port_busy "$VPN_PORT"; do
                VPN_PORT=$((VPN_PORT + 1))
            done
        fi
        CADDY_PORT=8443
        while port_busy "$CADDY_PORT" || [[ "$CADDY_PORT" == "$VPN_PORT" ]]; do
            CADDY_PORT=$((CADDY_PORT + 1))
        done
        HOLDER=$(port_holder 443)
        env_set COMPOSE_PROFILES caddy,sni
        env_set CADDYFILE Caddyfile.sni
        env_set CADDY_HTTPS_PUBLISH "127.0.0.1:$CADDY_PORT:8443"
        env_set CADDY_LOCAL_PORT "$CADDY_PORT"
        env_set VPN_PORT "$VPN_PORT"
        if port_busy 443; then
            STEP1="1. Порт 443 сейчас занят ($HOLDER). Переведите эту программу на порт $VPN_PORT.
   3x-ui: Inbounds → подключение с портом 443 → Edit → Port: $VPN_PORT → Save.
   Клиентам VPN ничего менять не нужно: они по-прежнему подключаются к 443.
   (До шага 2 VPN работать не будет.)"
        else
            STEP1="1. Порт 443 свободен. VPN должен слушать порт $VPN_PORT (сейчас: $(port_busy "$VPN_PORT" && echo "занят — похоже, это он" || echo "свободен"))."
        fi
        cat <<EOF

Режим «общий 443»: claude.ai подключается только к порту 443, поэтому на нём встанет HAProxy.
Соединения для $DOMAIN он отправит серверу Claude, все остальные — вашему VPN на порт $VPN_PORT.

$STEP1
2. Запуск:  docker compose up -d --build
3. Проверка:  curl https://$DOMAIN/health   (ответ: ok) — и проверьте, что VPN работает.

  URL коннектора:  https://$DOMAIN/mcp
  Пароль и токен:  cat .env

Откат: docker compose stop sni и верните VPN порт 443.
EOF
        exit 0
    fi

    env_set COMPOSE_PROFILES caddy
    env_set CADDYFILE Caddyfile
    env_set CADDY_HTTPS_PUBLISH 443:443
    cat <<EOF

Порты 80 и 443 свободны — HTTPS сделает встроенный Caddy.

  Запуск:          docker compose up -d --build
  URL коннектора:  https://$DOMAIN/mcp
  Пароль и токен:  cat .env
EOF
    exit 0
fi

# Порт 80 занят — HTTPS делает веб-сервер, который его держит.
env_set COMPOSE_PROFILES ""
sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PORT__/$LOCAL_PORT/g" nginx-site.conf.template > nginx-site.conf
HOLDER=$(port_holder 80)

cat <<EOF

Порт 80 занят ($HOLDER), поэтому встроенный Caddy выключен.
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
(тогда HTTPS сделает встроенный Caddy).
Если это ваш прокси для других сайтов — направьте в нём домен $DOMAIN
на 127.0.0.1:$LOCAL_PORT (пример для nginx — в файле nginx-site.conf).
EOF
        ;;
    *)
        [[ "$HOLDER" == unknown ]] && echo "Узнать, какая программа занимает порт:  sudo ss -ltnp 'sport = :80'"
        cat <<EOF
Если эта программа не нужна на порту 80 — остановите её (или переведите на другой порт)
и запустите ./setup.sh ещё раз: тогда HTTPS сделает встроенный Caddy.
Если это ваш веб-сервер — направьте в нём домен $DOMAIN на 127.0.0.1:$LOCAL_PORT
(готовый пример для nginx: nginx-site.conf, остальное — в README).
EOF
        ;;
esac
