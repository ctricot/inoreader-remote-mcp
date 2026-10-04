"""The admin password gate, for the few routes a person opens in a browser.

HTTP Basic: the browser asks once and resends the credentials for the rest of the origin,
which is exactly what a redirect round trip through Inoreader needs — the callback arrives
already authenticated. The user name is ignored; there is one owner.

Unset `ADMIN_PASSWORD` means refused to everyone, not open to everyone.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import secrets

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from .config import Settings

REALM = 'Basic realm="inoreader-remote-mcp", charset="UTF-8"'

# A wrong password costs this long, which makes guessing pointless.
FAILED_PASSWORD_DELAY = 1.0


async def check(request: Request, settings: Settings) -> Response | None:
    """`None` when the request carries the admin password, the refusal to return otherwise."""
    if settings.admin_password is None:
        return PlainTextResponse("ADMIN_PASSWORD is not configured on this server.", 503)

    given = _password(request.headers.get("authorization", ""))
    expected = settings.admin_password.get_secret_value().encode()
    if given is not None and secrets.compare_digest(given, expected):
        return None
    if given is not None:
        await asyncio.sleep(FAILED_PASSWORD_DELAY)
    return PlainTextResponse("Authentication required.", 401, {"WWW-Authenticate": REALM})


def _password(header: str) -> bytes | None:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    _, sep, password = decoded.partition(b":")
    return password if sep else None
