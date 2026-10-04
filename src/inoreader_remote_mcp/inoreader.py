"""The Inoreader API client: OAuth tokens, refresh, quotas.

## Refresh

The access token is refreshed **before** it expires (five minutes ahead), and once more if
the API still answers 401 — a token can be revoked before its announced expiry. One retry,
not a loop: a second 401 means the authorisation itself is gone.

Inoreader does not document whether the refresh token rotates. A new one is stored when the
response carries it; otherwise the old one is kept.

## Quotas

Inoreader counts requests per day and per zone (zone 1: reads, zone 2: writes); the Pro
plan allows about a hundred of each. Every response's counters are logged, and a refusal
for quota becomes a `QuotaExceeded` whose message Claude can relay as is — with the time
until the counters reset, so that it does not try again in a loop.

## Errors

Messages are in French: they reach Claude, which reads them to the user.

## One HTTP client per request

A request opens its own `httpx.AsyncClient`. At a hundred calls a day, the connection pool
saves nothing, and a client bound to no event loop survives restarts and tests alike.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings
from .token_store import InoreaderTokens, TokenStore

AUTH_URL = "https://www.inoreader.com/oauth2/auth"
TOKEN_URL = "https://www.inoreader.com/oauth2/token"
API_BASE = "https://www.inoreader.com/reader/api/0/"
SCOPE = "read write"

# Refresh this many seconds before the announced expiry.
REFRESH_MARGIN = 300
TIMEOUT = httpx.Timeout(20.0)

log = logging.getLogger(__name__)


class InoreaderError(ToolError):
    """An error whose message is meant for Claude.

    A `ToolError` because that is the only kind whose text the SDK passes on to the client;
    any other exception reaches Claude as a bare « Error executing tool », which says
    neither « quota » nor « authorise again ».
    """


class NotAuthorized(InoreaderError):
    def __init__(self, settings: Settings, detail: str = "") -> None:
        start = settings.url("/oauth/inoreader/start")
        super().__init__(
            "Le serveur n'a pas (ou plus) accès au compte Inoreader"
            + (f" ({detail})" if detail else "")
            + f". Le propriétaire doit refaire l'autorisation sur {start}."
        )


class QuotaExceeded(InoreaderError):
    def __init__(self, zone: str, reset_after: int | None) -> None:
        self.zone = zone
        self.reset_after = reset_after
        kind = {"1": "lecture", "2": "écriture"}.get(zone, "inconnue")
        when = (
            f" Les compteurs se remettent à zéro dans {_duration(reset_after)}."
            if reset_after is not None
            else ""
        )
        super().__init__(
            f"Quota journalier de l'API Inoreader atteint (zone {zone}, {kind}).{when} "
            "Inutile de réessayer avant."
        )


@dataclass(frozen=True)
class Quota:
    zone1_usage: int | None
    zone1_limit: int | None
    zone2_usage: int | None
    zone2_limit: int | None
    reset_after: int | None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> Quota:
        def num(name: str) -> int | None:
            value = headers.get(name)
            try:
                return int(float(value)) if value is not None else None
            except ValueError:
                return None

        return cls(
            zone1_usage=num("X-Reader-Zone1-Usage"),
            zone1_limit=num("X-Reader-Zone1-Limit"),
            zone2_usage=num("X-Reader-Zone2-Usage"),
            zone2_limit=num("X-Reader-Zone2-Limit"),
            reset_after=num("X-Reader-Limits-Reset-After"),
        )

    def exhausted_zone(self) -> str | None:
        for zone, usage, limit in (
            ("1", self.zone1_usage, self.zone1_limit),
            ("2", self.zone2_usage, self.zone2_limit),
        ):
            if usage is not None and limit is not None and usage >= limit:
                return zone
        return None


class InoreaderClient:
    def __init__(
        self,
        settings: Settings,
        store: TokenStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store or TokenStore(settings.inoreader_tokens_path)
        self.clock = clock
        self.last_quota: Quota | None = None
        self._refresh_lock = asyncio.Lock()

    # --- OAuth ---------------------------------------------------------------

    def authorize_url(self, state: str) -> str:
        return f"{AUTH_URL}?" + urlencode(
            {
                "client_id": self._app_id(),
                "redirect_uri": self.settings.inoreader_redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "state": state,
            }
        )

    async def exchange_code(self, code: str) -> InoreaderTokens:
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.inoreader_redirect_uri,
            }
        )
        tokens = self._tokens_from(payload, previous=None)
        self.store.save(tokens)
        log.info("Inoreader authorised (scope: %s)", tokens.scope or "?")
        return tokens

    async def _refresh(self, stale: InoreaderTokens) -> InoreaderTokens:
        async with self._refresh_lock:
            # Another request may have refreshed while this one waited for the lock.
            current = self.store.load()
            if current is not None and current.access_token != stale.access_token:
                return current
            try:
                payload = await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": stale.refresh_token}
                )
            except InoreaderError as exc:
                raise NotAuthorized(self.settings, "rafraîchissement du jeton refusé") from exc
            tokens = self._tokens_from(payload, previous=stale)
            self.store.save(tokens)
            log.info("Inoreader access token refreshed")
            return tokens

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        data = {
            **data,
            "client_id": self._app_id(),
            "client_secret": self._app_key(),
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            response = await http.post(TOKEN_URL, data=data)
        if response.status_code != 200:
            # The body may echo the request; log the status and error code only.
            log.warning("Inoreader token endpoint answered %s", response.status_code)
            raise InoreaderError(
                f"Le serveur OAuth d'Inoreader a refusé la demande ({response.status_code})."
            )
        return response.json()

    def _tokens_from(
        self, payload: dict[str, Any], previous: InoreaderTokens | None
    ) -> InoreaderTokens:
        refresh = payload.get("refresh_token") or (previous.refresh_token if previous else "")
        if not refresh:
            raise InoreaderError("Inoreader n'a pas renvoyé de jeton de rafraîchissement.")
        return InoreaderTokens(
            access_token=payload["access_token"],
            refresh_token=refresh,
            expires_at=self.clock() + float(payload.get("expires_in", 3600)),
            scope=payload.get("scope") or (previous.scope if previous else ""),
        )

    async def _valid_tokens(self) -> InoreaderTokens:
        tokens = self.store.load()
        if tokens is None:
            raise NotAuthorized(self.settings, "aucune autorisation enregistrée")
        if tokens.expires_at - REFRESH_MARGIN <= self.clock():
            tokens = await self._refresh(tokens)
        return tokens

    def _app_id(self) -> str:
        if not self.settings.inoreader_app_id:
            raise InoreaderError("INOREADER_APP_ID n'est pas configuré sur le serveur.")
        return self.settings.inoreader_app_id

    def _app_key(self) -> str:
        if self.settings.inoreader_app_key is None:
            raise InoreaderError("INOREADER_APP_KEY n'est pas configuré sur le serveur.")
        return self.settings.inoreader_app_key.get_secret_value()

    # --- API -----------------------------------------------------------------

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET an API path, JSON in return."""
        response = await self.request("GET", path, params={"output": "json", **(params or {})})
        return response.json()

    async def post(self, path: str, data: Mapping[str, Any] | None = None) -> httpx.Response:
        """POST a form to an API path. List values are sent as repeated keys."""
        return await self.request("POST", path, data=data)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        tokens = await self._valid_tokens()
        response = await self._send(method, path, tokens, params, data)
        if response.status_code == 401:
            tokens = await self._refresh(tokens)
            response = await self._send(method, path, tokens, params, data)
            if response.status_code == 401:
                raise NotAuthorized(self.settings, "jeton refusé par l'API")

        quota = Quota.from_headers(response.headers)
        self._log_quota(quota)

        if response.status_code == 429 or (response.status_code == 403 and quota.exhausted_zone()):
            raise QuotaExceeded(quota.exhausted_zone() or "?", quota.reset_after)
        if response.status_code == 403:
            raise InoreaderError(
                "Inoreader refuse l'accès (403). L'API exige un abonnement Pro, "
                "ou l'autorisation n'inclut pas l'écriture."
            )
        if response.status_code >= 400:
            raise InoreaderError(f"L'API Inoreader a répondu {response.status_code} sur {path}.")
        return response

    async def _send(
        self,
        method: str,
        path: str,
        tokens: InoreaderTokens,
        params: Mapping[str, Any] | None,
        data: Mapping[str, Any] | None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=TIMEOUT) as http:
            try:
                return await http.request(
                    method,
                    path.lstrip("/"),
                    params=params,
                    data=data,
                    headers={"Authorization": f"Bearer {tokens.access_token}"},
                )
            except httpx.HTTPError as exc:
                raise InoreaderError(f"Inoreader injoignable ({type(exc).__name__}).") from exc

    def _log_quota(self, quota: Quota) -> None:
        if quota.zone1_limit is None and quota.zone2_limit is None:
            return
        self.last_quota = quota
        log.info(
            "Inoreader quota: zone1 %s/%s, zone2 %s/%s, reset in %ss",
            quota.zone1_usage,
            quota.zone1_limit,
            quota.zone2_usage,
            quota.zone2_limit,
            quota.reset_after,
        )


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"
