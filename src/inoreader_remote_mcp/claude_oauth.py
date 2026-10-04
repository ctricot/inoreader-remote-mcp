"""This server as an OAuth 2.1 authorisation server, for Claude's custom connectors.

## Why

Claude's remote connectors (web, desktop, mobile) require OAuth: they register themselves
(RFC 7591), send the person to consent, then exchange a code for a token (PKCE). The MCP SDK
provides that whole HTTP surface — metadata, `/register`, `/authorize`, `/token`, `/revoke` —
given a provider: the storage and the moment of consent. That is this file, without a
database and with one owner.

## Consent

`authorize()` issues no code. It records the request and sends the browser to
`/oauth/consent`, which shows **who is asking and where the code will go** (the redirect
host: `claude.ai`, or something that should not be there). Approving takes the admin
password, typed into the form.

A typed password rather than HTTP Basic, on purpose: a browser resends Basic credentials on
its own, so any page could make the owner's browser post an approval for a client the
attacker registered. A password field cannot be filled by a forged request.

## Storage

Registered clients and issued tokens live in `DATA_DIR/oauth.json` (private, atomic), so a
container update does not disconnect Claude. Tokens are stored as SHA-256 digests only.
Pending requests and codes stay in memory: they live ten minutes, and a restart in the
middle of a consent costs one more click.

## Lifetimes

A code lives ten minutes, an access token one hour, a refresh token sixty days and rotates
on use: a stolen refresh token is worth nothing once the legitimate client has renewed.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import admin
from .config import Settings
from .private_file import read_json, write_json

SCOPE = "inoreader"

CODE_TTL = 600
ACCESS_TTL = 3600
REFRESH_TTL = 60 * 86400

# Registration is open (Claude declares itself, it cannot be configured by hand) and opens
# nothing without consent. The cap only keeps the file from growing without bound.
MAX_CLIENTS = 50


CONSENT_PATH = "/oauth/consent"


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass
class _Pending:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float


class Provider:
    """The provider the SDK queries, and the consent decisions the screen records."""

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.clock = clock
        self.path = settings.data_dir / "oauth.json"
        self._pending: dict[str, _Pending] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._state: dict[str, Any] | None = None

    # --- Clients -------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        entry = self._data()["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(entry["info"]) if entry else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        data = self._data()
        clients = data["clients"]
        clients[client_info.client_id] = {
            # The declaration as it arrived: rebuilding it field by field would lose
            # whatever the specification adds next year.
            "info": client_info.model_dump(mode="json"),
            "registered_at": self.clock(),
        }
        while len(clients) > MAX_CLIENTS:
            oldest = min(clients, key=lambda cid: clients[cid]["registered_at"])
            self._forget_client(data, oldest)
        self._save()

    # --- Authorisation -------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        now = self.clock()
        self._pending = {k: p for k, p in self._pending.items() if p.expires_at > now}
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = _Pending(client, params, now + CODE_TTL)
        return self.settings.url(f"{CONSENT_PATH}?request={request_id}")

    def pending(self, request_id: str) -> _Pending | None:
        entry = self._pending.get(request_id)
        if entry is None or entry.expires_at <= self.clock():
            return None
        return entry

    def approve(self, request_id: str) -> str:
        """Issues the code — consent begets it, and nothing else — and returns the
        redirect back to the client, `state` included."""
        entry = self._pending.pop(request_id)
        code = secrets.token_urlsafe(32)
        self._codes[digest(code)] = AuthorizationCode(
            code=code,
            scopes=[SCOPE],
            expires_at=self.clock() + CODE_TTL,
            client_id=entry.client.client_id,
            code_challenge=entry.params.code_challenge,
            redirect_uri=entry.params.redirect_uri,
            redirect_uri_provided_explicitly=entry.params.redirect_uri_provided_explicitly,
            resource=entry.params.resource,
        )
        return construct_redirect_uri(
            str(entry.params.redirect_uri), code=code, state=entry.params.state
        )

    def deny(self, request_id: str) -> str:
        """`access_denied` back to the client, so that it can say « refused » instead of
        waiting for a code that will not come."""
        entry = self._pending.pop(request_id)
        return construct_redirect_uri(
            str(entry.params.redirect_uri),
            error="access_denied",
            error_description="The owner refused the access.",
            state=entry.params.state,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(digest(authorization_code))
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at <= self.clock():
            self._codes.pop(digest(authorization_code), None)
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """PKCE is verified by the SDK before this call. A code is used once."""
        if self._codes.pop(digest(authorization_code.code), None) is None:
            raise ValueError("Authorization code already used.")
        return self._issue(client.client_id, authorization_code.resource)

    # --- Refresh -------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        entry = self._live(self._data()["refresh"], digest(refresh_token))
        if entry is None or entry["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=entry["client_id"],
            scopes=[SCOPE],
            expires_at=int(entry["expires_at"]),
            resource=entry.get("resource"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotation: the refresh token that asked is revoked, with its access tokens."""
        data = self._data()
        old = digest(refresh_token.token)
        if data["refresh"].pop(old, None) is None:
            raise ValueError("Refresh token unusable.")
        data["access"] = {k: a for k, a in data["access"].items() if a.get("refresh") != old}
        return self._issue(client.client_id, refresh_token.resource)

    # --- Tokens --------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        entry = self._live(self._data()["access"], digest(token))
        if entry is None:
            return None
        return AccessToken(
            token=token,
            client_id=entry["client_id"],
            scopes=[SCOPE],
            expires_at=int(entry["expires_at"]),
            resource=entry.get("resource"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revokes the pair, whichever half is given."""
        data = self._data()
        key = digest(token.token)
        access = data["access"].pop(key, None)
        refresh_key = access.get("refresh") if access else key
        data["refresh"].pop(refresh_key, None)
        data["access"] = {
            k: a for k, a in data["access"].items() if a.get("refresh") != refresh_key
        }
        self._save()

    # --- Internals -----------------------------------------------------------

    def _issue(self, client_id: str, resource: str | None) -> OAuthToken:
        now = self.clock()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        data = self._data()
        data["refresh"][digest(refresh)] = {
            "client_id": client_id,
            "expires_at": now + REFRESH_TTL,
            "resource": resource,
        }
        data["access"][digest(access)] = {
            "client_id": client_id,
            "expires_at": now + ACCESS_TTL,
            "resource": resource,
            "refresh": digest(refresh),
        }
        self._save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=SCOPE,
            refresh_token=refresh,
        )

    def _live(self, table: dict[str, Any], key: str) -> dict[str, Any] | None:
        entry = table.get(key)
        if entry is None or entry["expires_at"] <= self.clock():
            return None
        return entry

    def _forget_client(self, data: dict[str, Any], client_id: str) -> None:
        data["clients"].pop(client_id, None)
        for table in ("access", "refresh"):
            data[table] = {k: v for k, v in data[table].items() if v["client_id"] != client_id}

    def _data(self) -> dict[str, Any]:
        if self._state is None:
            loaded = read_json(self.path) or {}
            self._state = {
                "clients": loaded.get("clients", {}),
                "access": loaded.get("access", {}),
                "refresh": loaded.get("refresh", {}),
            }
        return self._state

    def _save(self) -> None:
        data = self._data()
        now = self.clock()
        for table in ("access", "refresh"):
            data[table] = {k: v for k, v in data[table].items() if v["expires_at"] > now}
        write_json(self.path, data)


# --- The consent screen ------------------------------------------------------------


def register_consent(server: MCPServer, settings: Settings, provider: Provider) -> None:
    @server.custom_route(CONSENT_PATH, methods=["GET"])
    async def consent_form(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        entry = provider.pending(request_id)
        if entry is None:
            return _expired()
        return _consent_page(request_id, entry)

    @server.custom_route(CONSENT_PATH, methods=["POST"])
    async def consent_answer(request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("request", ""))
        entry = provider.pending(request_id)
        if entry is None:
            return _expired()

        if form.get("decision") != "approve":
            return RedirectResponse(provider.deny(request_id), status_code=303)

        if settings.admin_password is None:
            return _consent_page(
                request_id, entry, "ADMIN_PASSWORD n'est pas configuré sur le serveur.", 503
            )
        given = str(form.get("password", "")).encode()
        expected = settings.admin_password.get_secret_value().encode()
        if not secrets.compare_digest(given, expected):
            await asyncio.sleep(admin.FAILED_PASSWORD_DELAY)
            return _consent_page(request_id, entry, "Mot de passe incorrect.", 401)

        return RedirectResponse(provider.approve(request_id), status_code=303)


def _consent_page(
    request_id: str, entry: _Pending, error: str | None = None, status: int = 200
) -> HTMLResponse:
    name = entry.client.client_name or entry.client.client_id
    host = urlparse(str(entry.params.redirect_uri)).netloc
    alert = f"<p class=error>{html.escape(error)}</p>" if error else ""
    body = f"""
<h1>Autoriser l'accès à Inoreader</h1>
<p><strong>{html.escape(name)}</strong> demande à lire et gérer votre compte Inoreader
(articles, abonnements, marquage lu/non lu).</p>
<p>Le code d'accès sera envoyé à <strong>{html.escape(host)}</strong>.
Si vous n'êtes pas à l'origine de cette demande, refusez.</p>
{alert}
<form method=post>
  <input type=hidden name=request value="{html.escape(request_id)}">
  <label>Mot de passe administrateur
    <input type=password name=password autocomplete=current-password autofocus>
  </label>
  <div class=actions>
    <button name=decision value=approve>Autoriser</button>
    <button name=decision value=deny class=secondary formnovalidate>Refuser</button>
  </div>
</form>"""
    return _html("Autoriser l'accès à Inoreader", body, status)


def _expired() -> HTMLResponse:
    return _html(
        "Demande expirée",
        "<h1>Demande expirée ou inconnue</h1><p>Relancez la connexion depuis Claude.</p>",
        400,
    )


def _html(title: str, body: str, status: int) -> HTMLResponse:
    page = f"""<!doctype html><html lang=fr><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; max-width: 30rem; margin: 3rem auto;
         padding: 0 1rem; line-height: 1.5; }}
  label {{ display: block; margin: 1.5rem 0 1rem; }}
  input[type=password] {{ display: block; width: 100%; box-sizing: border-box;
         padding: .6rem; margin-top: .4rem; font-size: 1rem; }}
  .actions {{ display: flex; gap: .75rem; }}
  button {{ padding: .6rem 1.2rem; font-size: 1rem; cursor: pointer; }}
  .error {{ color: #c62828; font-weight: 600; }}
</style>
<body>{body}</body></html>"""
    return HTMLResponse(page, status_code=status)
