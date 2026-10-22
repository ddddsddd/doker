#!/usr/bin/env bash
# Создаёт .env со случайным паролем и токеном.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
    echo "Файл .env уже есть. Удалите его, если хотите сгенерировать заново."
    exit 1
fi

read -rp "Домен сервера (например mcp.example.com): " DOMAIN
if [[ -z "$DOMAIN" ]]; then
    echo "Домен обязателен."
    exit 1
fi

gen() {
    local s
    s=$(head -c 96 /dev/urandom | base64 | tr -d '/+=\n')
    echo "${s:0:$1}"
}
ADMIN_PASSWORD=$(gen 24)
API_TOKEN=$(gen 48)

umask 077
cat > .env <<EOF
DOMAIN=$DOMAIN
ADMIN_PASSWORD=$ADMIN_PASSWORD
API_TOKEN=$API_TOKEN
EOF
mkdir -p workspace data

cat <<EOF

Готово, настройки сохранены в .env (доступен только вам).

  URL коннектора:  https://$DOMAIN/mcp
  Пароль:          $ADMIN_PASSWORD
  API-токен:       $API_TOKEN

Запуск:  docker compose up -d --build
EOF
