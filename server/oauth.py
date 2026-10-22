"""OAuth 2.1 authorization server for MCP clients (claude.ai connectors).

Clients register dynamically, then the owner approves each connection on a
login page with ADMIN_PASSWORD. Tokens are stored hashed in a JSON file so
connectors survive container restarts.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 24 * 3600
CODE_TTL = 5 * 60
PENDING_TTL = 15 * 60
MAX_CLIENTS = 200
STATIC_CLIENT_ID = "api-token"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PasswordOAuthProvider:
    def __init__(self, state_file: Path, public_url: str, api_token: str | None):
        self.state_file = state_file
        self.public_url = public_url.rstrip("/")
        self.api_token = api_token or None
        # Short-lived, in memory: authorization requests waiting for the password, and issued codes.
        self.pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams, float]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self._load()

    # ---- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        data = json.loads(self.state_file.read_text())
        self.clients = {
            cid: OAuthClientInformationFull.model_validate(info) for cid, info in data.get("clients", {}).items()
        }
        self.access = data.get("access", {})
        self.refresh = data.get("refresh", {})

    def _save(self) -> None:
        now = time.time()
        self.access = {h: t for h, t in self.access.items() if t["expires_at"] > now}
        self.refresh = {h: t for h, t in self.refresh.items() if t["expires_at"] > now}
        data = {
            "clients": {cid: c.model_dump(mode="json", exclude_none=True) for cid, c in self.clients.items()},
            "access": self.access,
            "refresh": self.refresh,
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(self.state_file)

    # ---- clients -----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if len(self.clients) >= MAX_CLIENTS:
            in_use = {t["client_id"] for t in self.refresh.values()}
            idle = sorted(
                (c for c in self.clients.values() if c.client_id not in in_use),
                key=lambda c: c.client_id_issued_at or 0,
            )
            for c in idle[: len(self.clients) - MAX_CLIENTS + 1]:
                del self.clients[c.client_id]
            if len(self.clients) >= MAX_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "Too many registered clients")
        self.clients[client_info.client_id] = client_info
        self._save()

    # ---- authorization (password page) -------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v[2] > now}
        request_id = secrets.token_urlsafe(32)
        self.pending[request_id] = (client, params, now + PENDING_TTL)
        return f"{self.public_url}/login?req={request_id}"

    def get_pending(self, request_id: str) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
        entry = self.pending.get(request_id)
        if not entry or entry[2] < time.time():
            return None
        return entry[0], entry[1]

    def approve(self, request_id: str) -> str:
        """Issue an authorization code and return the client's redirect URL."""
        client, params, _ = self.pending.pop(request_id)
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, request_id: str) -> str:
        _, params, _ = self.pending.pop(request_id)
        return construct_redirect_uri(
            str(params.redirect_uri), error="access_denied", error_description="Denied by owner", state=params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        if not code or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    # ---- tokens ------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        access_h, refresh_h = _hash(access), _hash(refresh)
        common = {"client_id": client_id, "scopes": scopes, "resource": resource}
        self.access[access_h] = {**common, "expires_at": now + ACCESS_TTL, "refresh": refresh_h}
        self.refresh[refresh_h] = {**common, "expires_at": now + REFRESH_TTL, "access": access_h}
        self._save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        rec = self.refresh.get(_hash(refresh_token))
        if not rec or rec["client_id"] != client.client_id or rec["expires_at"] < time.time():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=rec["client_id"],
            scopes=rec["scopes"],
            expires_at=rec["expires_at"],
            resource=rec["resource"],
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        await self.revoke_token(refresh_token)
        return self._issue(client.client_id, scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.api_token and hmac.compare_digest(token.encode(), self.api_token.encode()):
            return AccessToken(token=token, client_id=STATIC_CLIENT_ID, scopes=[], subject="owner")
        rec = self.access.get(_hash(token))
        if not rec or rec["expires_at"] < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=rec["client_id"],
            scopes=rec["scopes"],
            expires_at=rec["expires_at"],
            resource=rec["resource"],
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        h = _hash(token.token)
        access = self.access.pop(h, None)
        refresh = self.refresh.pop(h, None)
        if access:
            self.refresh.pop(access.get("refresh", ""), None)
        if refresh:
            self.access.pop(refresh.get("access", ""), None)
        self._save()
