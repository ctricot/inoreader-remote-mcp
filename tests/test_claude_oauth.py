"""The connector flow as Claude runs it: discover, register, authorise, consent, exchange,
call, refresh, revoke."""

import asyncio
import base64
import hashlib
import secrets
import stat
from urllib.parse import parse_qs, urlparse

import pytest

from inoreader_remote_mcp import admin, claude_oauth
from inoreader_remote_mcp.claude_oauth import Provider

from .conftest import ADMIN_PASSWORD

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
PING = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


@pytest.fixture(autouse=True)
def no_password_delay(monkeypatch):
    monkeypatch.setattr(admin, "FAILED_PASSWORD_DELAY", 0)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def register(http, auth_method: str = "none") -> dict:
    response = http.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": auth_method,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def authorize(http, client: dict, challenge: str) -> str:
    """Returns the consent request id."""
    response = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "the-state",
            "resource": "http://localhost/mcp",
        },
    )
    assert response.status_code == 302, response.text
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        "http://localhost/oauth/consent"
    )
    return parse_qs(location.query)["request"][0]


def consent(http, request_id: str, decision: str = "approve", password: str = ADMIN_PASSWORD):
    return http.post(
        "/oauth/consent",
        data={"request": request_id, "decision": decision, "password": password},
    )


def connect(http, auth_method: str = "none") -> tuple[dict, dict]:
    """The whole flow, returns (client, tokens)."""
    client = register(http, auth_method)
    secret = {"client_secret": client["client_secret"]} if "client_secret" in client else {}
    verifier, challenge = pkce()
    answer = consent(http, authorize(http, client, challenge))
    assert answer.status_code == 303
    code = parse_qs(urlparse(answer.headers["location"]).query)["code"][0]
    response = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client["client_id"],
            "code_verifier": verifier,
            **secret,
        },
    )
    assert response.status_code == 200, response.text
    return client, response.json()


def mcp(http, access_token: str):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {access_token}"}
    return http.post("/mcp", headers=headers, json=PING)


def test_metadata_is_discoverable(http):
    resource = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == "http://localhost/mcp"
    assert resource["authorization_servers"] == ["http://localhost"]
    assert resource["scopes_supported"] == ["inoreader"]

    server = http.get("/.well-known/oauth-authorization-server").json()
    assert server["issuer"] == "http://localhost"
    assert server["authorization_endpoint"] == "http://localhost/authorize"
    assert server["token_endpoint"] == "http://localhost/token"
    assert server["registration_endpoint"] == "http://localhost/register"
    assert "S256" in server["code_challenge_methods_supported"]


def test_full_flow_gives_access_to_the_tools(http):
    _, tokens = connect(http)
    assert tokens["token_type"].lower() == "bearer"
    assert tokens["scope"] == "inoreader"
    assert tokens["refresh_token"]

    response = mcp(http, tokens["access_token"])
    assert response.status_code == 200
    assert "get_articles" in response.text


def test_consent_page_names_the_client_and_the_redirect_host(http):
    _, challenge = pkce()
    request_id = authorize(http, register(http), challenge)
    page = http.get(f"/oauth/consent?request={request_id}")
    assert page.status_code == 200
    assert "Claude" in page.text and "claude.ai" in page.text
    assert "type=password" in page.text


def test_wrong_password_issues_nothing(http):
    _, challenge = pkce()
    request_id = authorize(http, register(http), challenge)
    answer = consent(http, request_id, password="wrong")
    assert answer.status_code == 401
    assert "incorrect" in answer.text
    # The request is still pending: the owner can type the password again.
    assert consent(http, request_id).status_code == 303


def test_deny_returns_access_denied_with_state(http):
    _, challenge = pkce()
    request_id = authorize(http, register(http), challenge)
    answer = consent(http, request_id, decision="deny", password="")
    assert answer.status_code == 303
    params = parse_qs(urlparse(answer.headers["location"]).query)
    assert params["error"] == ["access_denied"]
    assert params["state"] == ["the-state"]
    # A request is answered once.
    assert consent(http, request_id).status_code == 400


def test_approve_returns_code_and_state(http):
    _, challenge = pkce()
    answer = consent(http, authorize(http, register(http), challenge))
    location = urlparse(answer.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
    assert parse_qs(location.query)["state"] == ["the-state"]


def test_unknown_or_expired_request(http, clock):
    assert http.get("/oauth/consent?request=nope").status_code == 400
    _, challenge = pkce()
    request_id = authorize(http, register(http), challenge)
    clock.now += claude_oauth.CODE_TTL + 1
    assert consent(http, request_id).status_code == 400


def test_consent_closed_without_admin_password(http, settings):
    _, challenge = pkce()
    request_id = authorize(http, register(http), challenge)
    settings.admin_password = None
    assert consent(http, request_id).status_code == 503


def test_code_is_single_use_and_needs_the_verifier(http):
    client = register(http)
    verifier, challenge = pkce()
    answer = consent(http, authorize(http, client, challenge))
    code = parse_qs(urlparse(answer.headers["location"]).query)["code"][0]
    exchange = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client["client_id"],
    }
    assert http.post("/token", data={**exchange, "code_verifier": "wrong"}).status_code == 400
    assert http.post("/token", data={**exchange, "code_verifier": verifier}).status_code == 200
    assert http.post("/token", data={**exchange, "code_verifier": verifier}).status_code == 400


def test_access_token_expires(http, clock):
    _, tokens = connect(http)
    clock.now += claude_oauth.ACCESS_TTL + 1
    assert mcp(http, tokens["access_token"]).status_code == 401


def test_refresh_rotates(http):
    client, tokens = connect(http)
    refresh = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client["client_id"],
    }
    renewed = http.post("/token", data=refresh)
    assert renewed.status_code == 200, renewed.text
    new = renewed.json()

    assert mcp(http, new["access_token"]).status_code == 200
    # The old pair is dead.
    assert mcp(http, tokens["access_token"]).status_code == 401
    assert http.post("/token", data=refresh).status_code == 400


def test_revocation_kills_the_pair(http):
    # The SDK's /revoke wants a client secret: a confidential client, as registered here.
    client, tokens = connect(http, "client_secret_post")
    auth = {"client_id": client["client_id"], "client_secret": client["client_secret"]}
    response = http.post("/revoke", data={"token": tokens["access_token"], **auth})
    assert response.status_code == 200, response.text
    assert mcp(http, tokens["access_token"]).status_code == 401
    refresh = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], **auth}
    assert http.post("/token", data=refresh).status_code == 400


def test_tokens_survive_a_restart_and_are_stored_hashed(http, settings, clock):
    client, tokens = connect(http)
    path = settings.data_dir / "oauth.json"
    content = path.read_text()
    assert tokens["access_token"] not in content
    assert tokens["refresh_token"] not in content
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    restarted = Provider(settings, clock=clock)
    assert asyncio.run(restarted.get_client(client["client_id"])) is not None
    assert asyncio.run(restarted.load_access_token(tokens["access_token"])) is not None


async def test_client_registrations_are_capped(settings, clock, monkeypatch):
    from mcp.shared.auth import OAuthClientInformationFull

    monkeypatch.setattr(claude_oauth, "MAX_CLIENTS", 2)
    provider = Provider(settings, clock=clock)
    for i in range(3):
        clock.now += 1
        await provider.register_client(
            OAuthClientInformationFull(client_id=f"c{i}", redirect_uris=[REDIRECT])
        )
    assert await provider.get_client("c0") is None
    assert await provider.get_client("c2") is not None
