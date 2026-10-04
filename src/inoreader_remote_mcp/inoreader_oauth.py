"""The one-time Inoreader authorisation, done by the owner in a browser.

`/oauth/inoreader/start` sends the browser to Inoreader; Inoreader sends it back to
`/oauth/inoreader/callback` with a code, exchanged for tokens stored on the volume. Both
routes sit behind the admin password: anyone able to run the flow could bind the server
to *their* Inoreader account.

The `state` lives in memory for ten minutes. A restart in the middle of an authorisation
loses it, and the owner clicks again — not worth a file.
"""

from __future__ import annotations

import html
import logging
import secrets
import time

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import admin
from .config import Settings
from .inoreader import InoreaderClient, InoreaderError

STATE_TTL = 600

log = logging.getLogger(__name__)


def register(server: MCPServer, settings: Settings, client: InoreaderClient) -> None:
    pending: dict[str, float] = {}

    def remember_state() -> str:
        now = time.monotonic()
        for expired in [s for s, until in pending.items() if until < now]:
            del pending[expired]
        state = secrets.token_urlsafe(32)
        pending[state] = now + STATE_TTL
        return state

    def consume_state(state: str | None) -> bool:
        until = pending.pop(state or "", None)
        return until is not None and until >= time.monotonic()

    @server.custom_route("/oauth/inoreader/start", methods=["GET"])
    async def start(request: Request) -> Response:
        if refusal := await admin.check(request, settings):
            return refusal
        try:
            url = client.authorize_url(remember_state())
        except InoreaderError as exc:
            return _page("Configuration incomplète", str(exc), 500)
        return RedirectResponse(url, status_code=302)

    @server.custom_route("/oauth/inoreader/callback", methods=["GET"])
    async def callback(request: Request) -> Response:
        if refusal := await admin.check(request, settings):
            return refusal
        params = request.query_params
        if not consume_state(params.get("state")):
            return _page(
                "Demande expirée ou inconnue",
                "Relancez l'autorisation depuis /oauth/inoreader/start.",
                400,
            )
        if error := params.get("error"):
            return _page("Autorisation refusée", f"Inoreader a répondu : {error}.", 400)
        code = params.get("code")
        if not code:
            return _page("Réponse incomplète", "Inoreader n'a pas renvoyé de code.", 400)
        try:
            tokens = await client.exchange_code(code)
        except InoreaderError as exc:
            log.warning("Inoreader code exchange failed: %s", exc)
            return _page("Échec de l'autorisation", str(exc), 502)
        return _page(
            "Inoreader est connecté",
            f"Jetons enregistrés (portée : {tokens.scope or 'non précisée'}). "
            "Vous pouvez fermer cette page.",
            200,
        )


def _page(title: str, message: str, status: int) -> HTMLResponse:
    body = (
        "<!doctype html><html lang=fr><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<body style='font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )
    return HTMLResponse(body, status_code=status)
