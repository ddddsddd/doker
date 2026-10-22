# Claude ↔ ваш сервер (MCP в Docker)

Docker-контейнер, через который Claude (claude.ai, приложение на телефоне, Claude Desktop и Claude Code)
выполняет команды и работает с файлами на вашем Linux-сервере.

Внутри MCP-сервер с инструментами:

| Инструмент | Что делает |
|---|---|
| `run_command` | выполняет bash-команду, возвращает код выхода, stdout и stderr |
| `read_file` / `write_file` / `edit_file` | чтение, запись и точечная правка файлов |
| `list_directory` / `search_files` | просмотр папок, поиск по именам и содержимому |
| `move_path` / `delete_path` | перемещение, переименование, удаление |

Подключение защищено OAuth: при добавлении коннектора claude.ai открывает страницу входа на **вашем**
сервере, и доступ выдаётся только после ввода вашего пароля. Каждое действие Claude пишется в журнал.

## Что нужно

- Linux-сервер с Docker и Docker Compose (`docker compose version` должен работать).
- Домен (или поддомен), A-запись которого указывает на IP сервера. Нет домена — подойдёт бесплатный
  поддомен, например на [duckdns.org](https://www.duckdns.org).
- HTTPS — claude.ai подключается только по нему. Если порты 80 и 443 свободны, сертификат автоматически
  получит встроенный Caddy. Если их уже занимает nginx/apache, `setup.sh` это заметит и подготовит
  настройку через ваш веб-сервер (см. [Порты 80/443 заняты](#порты-80443-заняты)).

## Установка

```bash
git clone https://github.com/ddddsddd/doker.git
cd doker
./setup.sh                    # спросит домен, создаст пароль и токен, проверит порты 80/443
docker compose up -d --build
```

Если `setup.sh` сообщил, что порты заняты, выполните шаги, которые он вывел (они же описаны ниже
в разделе «Порты 80/443 заняты»).

Проверка: `curl http://127.0.0.1:8000/health` на самом сервере и `curl https://ВАШ_ДОМЕН/health`
снаружи должны ответить `ok`. Пароль и токен потом можно посмотреть командой `cat .env`.

## Подключение к claude.ai

1. Откройте claude.ai → **Settings → Connectors → Add custom connector**
   (названия пунктов в интерфейсе могут немного отличаться).
2. Название — любое, например «Мой сервер». URL — `https://ВАШ_ДОМЕН/mcp`.
3. Нажмите **Connect**. Откроется страница «Разрешить доступ к серверу?» — введите `ADMIN_PASSWORD`
   из `.env` и нажмите «Разрешить».

Готово: попросите Claude, например, «покажи, сколько места на диске» или «создай файл hello.txt».

По умолчанию Claude спрашивает подтверждение перед каждым вызовом инструмента. В настройках коннектора
можно разрешить отдельные инструменты навсегда — например, только чтение файлов.

## Как пользоваться в других чатах

Коннектор привязан к вашему **аккаунту**, а не к одному чату, поэтому он доступен везде:

- **Любой новый чат на claude.ai:** кнопка **«+»** (или значок инструментов) под полем ввода →
  включите коннектор «Мой сервер».
- **Проекты (Projects):** работает так же, включается в чате проекта.
- **Приложение на телефоне и Claude Desktop:** коннекторы, добавленные на claude.ai, появляются там сами
  после входа в тот же аккаунт.
- **Claude Code (терминал)** — подключается по `API_TOKEN`, флаг `--scope user` делает сервер
  доступным во всех проектах:

  ```bash
  claude mcp add --transport http --scope user my-server https://ВАШ_ДОМЕН/mcp \
    --header "Authorization: Bearer ВАШ_API_TOKEN"
  ```

## Доступ к файлам самого сервера

По умолчанию Claude работает **внутри контейнера**: видит папку `./workspace` (внутри — `/workspace`)
и не может трогать остальную систему. Чтобы дать доступ к папкам сервера, раскомментируйте нужные
строки в `docker-compose.yml` и выполните `docker compose up -d`:

```yaml
- /home:/host/home        # папка сервера будет видна Claude как /host/home
- /:/host                 # весь диск — Claude сможет изменить что угодно
- /var/run/docker.sock:/var/run/docker.sock   # управление Docker = root-доступ к серверу
```

Давайте ровно тот доступ, который нужен для ваших задач.

## Безопасность

- Без пароля или токена сервер отвечает `401`. Пароль защищён от перебора: после 5 ошибок вход
  блокируется на 15 минут.
- Токены хранятся в `data/oauth.json` только в виде хешей. Пароль и токен не передаются в
  окружение команд, которые запускает Claude.
- Журнал действий: `data/logs/audit.log` — время, клиент, инструмент и аргументы каждого вызова.
- Не публикуйте `.env` (он уже в `.gitignore`).

**Отозвать все подключения claude.ai:**

```bash
docker compose stop mcp && rm data/oauth.json && docker compose start mcp
```

Чтобы сменить пароль или токен, отредактируйте `.env` и выполните `docker compose up -d`.

## Обслуживание

```bash
docker compose logs -f mcp          # логи сервера
tail -f data/logs/audit.log         # что делал Claude
git pull && docker compose up -d --build   # обновление
docker compose down                 # остановка
```

## Порты 80/443 заняты

Ошибка `failed to bind host port 0.0.0.0:80/tcp: address already in use` при запуске означает, что
порт уже занят другой программой — обычно nginx или apache. Запустите ещё раз:

```bash
docker compose down
git pull
./setup.sh
```

Скрипт сохранит пароль и токен, определит, кто занимает порт, выключит встроенный Caddy
(`COMPOSE_PROFILES=` в `.env`) и напишет, что делать дальше. MCP-сервер в этом режиме доступен только
с самого сервера, на `127.0.0.1:8000` (номер порта — `MCP_LOCAL_PORT` в `.env`), а HTTPS делает
ваш веб-сервер.

Узнать вручную, кто занимает порт: `sudo ss -ltnp 'sport = :80'`. Если это стандартный nginx/apache,
на котором нет ваших сайтов, проще всего его выключить (`sudo systemctl disable --now nginx`)
и снова запустить `./setup.sh` — тогда HTTPS сделает встроенный Caddy.

### Порт 443 занят VPN (Xray, 3x-ui, Marzban) или другой программой

Если порт 80 свободен, VPN трогать не нужно: `setup.sh` сам включит Caddy на другом порту —
первом свободном начиная с 8443 (`HTTPS_PORT` в `.env`). Сертификат Caddy получает через порт 80,
поэтому порт 80 должен оставаться свободным. Адрес коннектора тогда будет с портом:

```
https://ВАШ_ДОМЕН:8443/mcp
```

Если у хостинга есть файрвол (security group), откройте в нём порты 80 и 8443.

Если порт 80 занят программой, которая вам не нужна (например, старым сайтом или игрой),
остановите её или переведите на другой порт и запустите `./setup.sh` ещё раз.

### nginx

`setup.sh` создаёт готовый `nginx-site.conf` с вашим доменом (шаблон — `nginx-site.conf.template`):

```bash
docker compose up -d --build
sudo cp nginx-site.conf /etc/nginx/sites-available/claude-mcp.conf
sudo ln -sf /etc/nginx/sites-available/claude-mcp.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx   # если certbot ещё нет
sudo certbot --nginx -d ВАШ_ДОМЕН
```

Если nginx у вас с каталогом `conf.d` вместо `sites-available`, скопируйте файл в
`/etc/nginx/conf.d/claude-mcp.conf`.

### Apache

```bash
sudo a2enmod proxy proxy_http headers
sudo tee /etc/apache2/sites-available/claude-mcp.conf > /dev/null <<'EOF'
<VirtualHost *:80>
    ServerName ВАШ_ДОМЕН
    ProxyPreserveHost On
    RequestHeader unset X-Forwarded-For
    ProxyPass / http://127.0.0.1:8000/ flushpackets=on timeout=3600
    ProxyPassReverse / http://127.0.0.1:8000/
</VirtualHost>
EOF
sudo a2ensite claude-mcp && sudo systemctl reload apache2
sudo apt install -y certbot python3-certbot-apache
sudo certbot --apache -d ВАШ_ДОМЕН
```

### Caddy, установленный на сервере

Добавьте в его Caddyfile блок `ВАШ_ДОМЕН { reverse_proxy 127.0.0.1:8000 }` и выполните
`sudo systemctl reload caddy`.

## Настройки `.env`

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DOMAIN` | — | домен сервера (обязательно) |
| `ADMIN_PASSWORD` | — | пароль для подключения коннектора, от 12 символов (обязательно) |
| `API_TOKEN` | пусто | токен для Claude Code и других клиентов, от 32 символов |
| `COMPOSE_PROFILES` | — | `caddy` — встроенный HTTPS; пусто — HTTPS делает ваш nginx/apache |
| `MCP_LOCAL_PORT` | `8000` | порт на `127.0.0.1`, куда проксирует ваш веб-сервер |
| `HTTPS_PORT` | `443` | порт HTTPS встроенного Caddy, если 443 занят (например, VPN) |
| `COMMAND_TIMEOUT` | `120` | таймаут команды по умолчанию, секунд |
| `MAX_COMMAND_TIMEOUT` | `1800` | максимальный таймаут, который может запросить Claude |
| `PUBLIC_URL` | `https://$DOMAIN` | внешний адрес, если он отличается от домена |
