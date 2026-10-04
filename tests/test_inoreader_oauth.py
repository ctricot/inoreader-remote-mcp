from urllib.parse import parse_qs, urlparse

import pytest
import respx

from inoreader_remote_mcp import admin
from inoreader_remote_mcp.inoreader import AUTH_URL, TOKEN_URL

from .conftest import ADMIN_PASSWORD

AUTH = ("owner", ADMIN_PASSWORD)


@pytest.fixture(autouse=True)
def no_password_delay(monkeypatch):
    monkeypatch.setattr(admin, "FAILED_PASSWORD_DELAY", 0)


def start(http) -> str:
    response = http.get("/oauth/inoreader/start", auth=AUTH)
    assert response.status_code == 302
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


@pytest.mark.parametrize("path", ["/oauth/inoreader/start", "/oauth/inoreader/callback"])
def test_routes_require_the_admin_password(http, path):
    assert http.get(path).status_code == 401
    assert "Basic" in http.get(path).headers["www-authenticate"]
    assert http.get(path, auth=("owner", "wrong")).status_code == 401


def test_routes_are_closed_without_admin_password(http, settings):
    settings.admin_password = None
    assert http.get("/oauth/inoreader/start", auth=AUTH).status_code == 503


def test_start_redirects_to_inoreader(http):
    response = http.get("/oauth/inoreader/start", auth=AUTH)
    location = urlparse(response.headers["location"])
    params = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTH_URL
    assert params["client_id"] == ["app-id"]
    assert params["redirect_uri"] == ["http://localhost/oauth/inoreader/callback"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["read write"]
    assert len(params["state"][0]) >= 32


def test_callback_rejects_unknown_state(http):
    response = http.get("/oauth/inoreader/callback?code=x&state=forged", auth=AUTH)
    assert response.status_code == 400


@respx.mock
def test_callback_stores_tokens(http, store):
    token = respx.post(TOKEN_URL).respond(
        json={
            "access_token": "a",
            "refresh_token": "r",
            "expires_in": 3600,
            "scope": "read write",
        }
    )
    state = start(http)

    response = http.get(f"/oauth/inoreader/callback?code=the-code&state={state}", auth=AUTH)

    assert response.status_code == 200
    sent = parse_qs(token.calls.last.request.content.decode())
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["the-code"]
    assert sent["redirect_uri"] == ["http://localhost/oauth/inoreader/callback"]
    assert store.load().refresh_token == "r"
    # A state is single use.
    replay = http.get(f"/oauth/inoreader/callback?code=the-code&state={state}", auth=AUTH)
    assert replay.status_code == 400


def test_callback_reports_denial(http, store):
    state = start(http)
    response = http.get(f"/oauth/inoreader/callback?error=access_denied&state={state}", auth=AUTH)
    assert response.status_code == 400
    assert "access_denied" in response.text
    assert store.load() is None


@respx.mock
def test_callback_reports_failed_exchange(http, store):
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    state = start(http)
    response = http.get(f"/oauth/inoreader/callback?code=bad&state={state}", auth=AUTH)
    assert response.status_code == 502
    assert store.load() is None
