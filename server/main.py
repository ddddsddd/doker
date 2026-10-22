"""Remote MCP server that lets Claude run commands and manage files on this server."""

import asyncio
import hmac
import html
import logging
import os
import sys
import time
from pathlib import Path

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from oauth import PasswordOAuthProvider
from tools import register_tools

log = logging.getLogger("claude-server")

SECRET_VARS = ("ADMIN_PASSWORD", "API_TOKEN")
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW = 15 * 60
TRUSTED_REDIRECT_HOSTS = {"claude.ai", "localhost", "127.0.0.1"}


def fail(msg: str) -> None:
    print(f"ОШИБКА КОНФИГУРАЦИИ: {msg}", file=sys.stderr)
    sys.exit(1)


domain = os.environ.get("DOMAIN", "").strip()
https_port = os.environ.get("HTTPS_PORT", "").strip()
public_url = os.environ.get("PUBLIC_URL", "").strip()
if not public_url and domain:
    public_url = f"https://{domain}" if https_port in ("", "443") else f"https://{domain}:{https_port}"
public_url = public_url.rstrip("/")
admin_password = os.environ.get("ADMIN_PASSWORD", "")
api_token = os.environ.get("API_TOKEN", "").strip()
workdir = Path(os.environ.get("WORKDIR", "/workspace"))
state_dir = Path(os.environ.get("STATE_DIR", "/data"))
port = int(os.environ.get("PORT", "8000"))

if not public_url:
    fail("задайте DOMAIN (или PUBLIC_URL) в .env")
if not (public_url.startswith("https://") or public_url.startswith("http://localhost")):
    fail("PUBLIC_URL должен начинаться с https://")
if len(admin_password) < 12:
    fail("ADMIN_PASSWORD должен быть не короче 12 символов")
if api_token and len(api_token) < 32:
    fail("API_TOKEN должен быть не короче 32 символов (или оставьте его пустым)")

workdir.mkdir(parents=True, exist_ok=True)
provider = PasswordOAuthProvider(state_dir / "oauth.json", public_url, api_token)

mcp = FastMCP(
    "server-control",
    instructions=(
        "Tools for administering the owner's Linux server from inside a Docker container. "
        f"The working directory is {workdir}; host directories, if the owner mounted any, are under /host. "
        "Use read_file/edit_file/write_file for files and run_command for everything else. "
        "Explain destructive actions before running them."
    ),
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=public_url,
        resource_server_url=f"{public_url}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=False,
    ),
    host="0.0.0.0",
    port=port,
)

register_tools(
    mcp,
    workdir=workdir,
    audit_log=state_dir / "logs" / "audit.log",
    default_timeout=int(os.environ.get("COMMAND_TIMEOUT", "120")),
    max_timeout=int(os.environ.get("MAX_COMMAND_TIMEOUT", "1800")),
    max_output=int(os.environ.get("MAX_OUTPUT_BYTES", "100000")),
    child_env={k: v for k, v in os.environ.items() if k not in SECRET_VARS},
)


# ---- login page that approves an OAuth connection -----------------------------

login_failures: dict[str, list[float]] = {}

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Подключение к серверу</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f4f4f5;color:#18181b;display:flex;justify-content:center;padding:48px 16px;margin:0}}
.card{{background:#fff;border-radius:12px;padding:28px;max-width:420px;width:100%;box-shadow:0 1px 3px #0002}}
h1{{font-size:20px;margin:0 0 12px}} p{{line-height:1.5;margin:0 0 12px}} code{{background:#f4f4f5;padding:2px 5px;border-radius:4px}}
input{{width:100%;box-sizing:border-box;padding:10px;font-size:16px;border:1px solid #d4d4d8;border-radius:8px;margin:8px 0 16px}}
.row{{display:flex;gap:8px}} button{{flex:1;padding:10px;font-size:16px;border-radius:8px;border:0;cursor:pointer}}
.ok{{background:#18181b;color:#fff}} .no{{background:#e4e4e7}} .err{{color:#b91c1c}}
</style></head><body><div class="card">
<h1>Разрешить доступ к серверу?</h1>
<p><b>{client}</b> запрашивает полный доступ: выполнение команд и работу с файлами.</p>
<p>После входа вы вернётесь на <code>{redirect_host}</code>.</p>
{warning}{error}
<form method="post">
<input type="hidden" name="req" value="{req}">
<label>Пароль администратора (ADMIN_PASSWORD)
<input type="password" name="password" autocomplete="current-password" autofocus></label>
<div class="row"><button class="ok" name="action" value="allow">Разрешить</button>
<button class="no" name="action" value="deny">Отклонить</button></div>
</form></div></body></html>"""

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def render_login(req: str, error: str = "", status: int = 200) -> Response:
    pending = provider.get_pending(req)
    if not pending:
        return PlainTextResponse("Запрос на подключение устарел. Начните подключение заново.", status_code=400)
    client, params = pending
    redirect_host = params.redirect_uri.host or ""
    page = PAGE.format(
        client=html.escape(client.client_name or client.client_id),
        redirect_host=html.escape(redirect_host),
        warning=""
        if redirect_host in TRUSTED_REDIRECT_HOSTS
        else '<p class="err">Внимание: это не claude.ai. Если подключение начинали не вы — нажмите «Отклонить».</p>',
        req=html.escape(req),
        error=f'<p class="err">{html.escape(error)}</p>' if error else "",
    )
    return HTMLResponse(page, status_code=status, headers=SECURITY_HEADERS)


@mcp.custom_route("/login", methods=["GET", "POST"])
async def login(request: Request) -> Response:
    if request.method == "GET":
        return render_login(request.query_params.get("req", ""))

    form = await request.form()
    req = str(form.get("req", ""))
    if not provider.get_pending(req):
        return render_login(req)
    if form.get("action") == "deny":
        return RedirectResponse(provider.deny(req), status_code=302)

    ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in login_failures.get(ip, []) if t > now - LOGIN_WINDOW]
    if len(recent) >= MAX_LOGIN_FAILURES:
        log.warning("login blocked for %s: too many failures", ip)
        return render_login(req, "Слишком много неудачных попыток. Подождите 15 минут.", 429)

    if not hmac.compare_digest(str(form.get("password", "")).encode(), admin_password.encode()):
        login_failures[ip] = recent + [now]
        log.warning("failed login from %s", ip)
        await asyncio.sleep(1)
        return render_login(req, "Неверный пароль.", 401)

    login_failures.pop(ip, None)
    client, _ = provider.get_pending(req)
    log.info("access granted to client %s (%s) from %s", client.client_name, client.client_id, ip)
    return RedirectResponse(provider.approve(req), status_code=302)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return PlainTextResponse("ok")


def main() -> None:
    log.info("MCP endpoint: %s/mcp", public_url)
    uvicorn.run(
        mcp.streamable_http_app(),
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        # Only proxies on loopback/private networks (Caddy, host nginx via Docker) may set
        # X-Forwarded-For; uvicorn then takes the rightmost untrusted address as the client.
        forwarded_allow_ips=os.environ.get(
            "FORWARDED_ALLOW_IPS", "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
        ),
        log_level="info",
    )


if __name__ == "__main__":
    main()
