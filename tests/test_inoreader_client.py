import logging
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from inoreader_remote_mcp.inoreader import (
    API_BASE,
    TOKEN_URL,
    InoreaderError,
    NotAuthorized,
    QuotaExceeded,
)

from .conftest import NOW

USER_INFO = f"{API_BASE}user-info"
QUOTA_HEADERS = {
    "X-Reader-Zone1-Usage": "12",
    "X-Reader-Zone1-Limit": "100",
    "X-Reader-Zone2-Usage": "3",
    "X-Reader-Zone2-Limit": "100",
    "X-Reader-Limits-Reset-After": "5400",
}


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


async def test_no_tokens_says_how_to_authorise(client):
    with pytest.raises(NotAuthorized, match="http://localhost/oauth/inoreader/start"):
        await client.get("user-info")


@respx.mock
async def test_get_sends_bearer_and_json_output(client, authorised):
    route = respx.get(USER_INFO).respond(json={"userName": "me"})
    assert await client.get("user-info") == {"userName": "me"}
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer access-1"
    assert request.url.params["output"] == "json"


@respx.mock
async def test_post_repeats_list_values(client, authorised):
    route = respx.post(f"{API_BASE}edit-tag").respond(text="OK")
    await client.post("edit-tag", {"a": "user/-/state/com.google/read", "i": ["1", "2"]})
    assert form(route.calls.last.request)["i"] == ["1", "2"]


@respx.mock
async def test_refreshes_before_expiry(client, store, clock, authorised):
    clock.now = authorised.expires_at - 60  # inside the five-minute margin
    token = respx.post(TOKEN_URL).respond(
        json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600}
    )
    api = respx.get(USER_INFO).respond(json={})

    await client.get("user-info")

    sent = form(token.calls.last.request)
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == ["refresh-1"]
    assert sent["client_id"] == ["app-id"] and sent["client_secret"] == ["app-key"]
    assert api.calls.last.request.headers["Authorization"] == "Bearer access-2"
    saved = store.load()
    assert saved.refresh_token == "refresh-2"
    assert saved.expires_at == clock.now + 3600


@respx.mock
async def test_401_refreshes_once_and_retries(client, store, authorised):
    respx.post(TOKEN_URL).respond(json={"access_token": "access-2", "expires_in": 3600})
    api = respx.get(USER_INFO).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": True})]
    )

    assert await client.get("user-info") == {"ok": True}
    assert api.call_count == 2
    # No new refresh token in the response: the old one is kept.
    assert store.load().refresh_token == "refresh-1"
    assert store.load().scope == "read write"


@respx.mock
async def test_second_401_means_not_authorised(client, authorised):
    respx.post(TOKEN_URL).respond(json={"access_token": "access-2", "expires_in": 3600})
    respx.get(USER_INFO).respond(401)
    with pytest.raises(NotAuthorized):
        await client.get("user-info")


@respx.mock
async def test_refused_refresh_means_not_authorised(client, clock, authorised):
    clock.now = NOW + 7200
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(NotAuthorized, match="rafraîchissement"):
        await client.get("user-info")


@respx.mock
async def test_quota_is_logged_and_kept(client, authorised, caplog):
    respx.get(USER_INFO).respond(json={}, headers=QUOTA_HEADERS)
    with caplog.at_level(logging.INFO):
        await client.get("user-info")
    assert "zone1 12/100, zone2 3/100, reset in 5400s" in caplog.text
    assert client.last_quota.zone1_usage == 12


@respx.mock
async def test_429_is_quota_exceeded_with_reset_time(client, authorised):
    respx.get(USER_INFO).respond(429, headers={**QUOTA_HEADERS, "X-Reader-Zone1-Usage": "100"})
    with pytest.raises(QuotaExceeded) as caught:
        await client.get("user-info")
    assert caught.value.zone == "1"
    assert "lecture" in str(caught.value) and "1 h 30 min" in str(caught.value)


@respx.mock
async def test_403_with_exhausted_zone_is_quota_exceeded(client, authorised):
    respx.post(f"{API_BASE}edit-tag").respond(
        403, headers={**QUOTA_HEADERS, "X-Reader-Zone2-Usage": "100"}
    )
    with pytest.raises(QuotaExceeded, match="écriture"):
        await client.post("edit-tag", {"i": "1"})


@respx.mock
async def test_plain_403_explains_pro_or_scope(client, authorised):
    respx.get(USER_INFO).respond(403, headers=QUOTA_HEADERS)
    with pytest.raises(InoreaderError, match="Pro") as caught:
        await client.get("user-info")
    assert not isinstance(caught.value, QuotaExceeded)


@respx.mock
async def test_network_error_is_reported(client, authorised):
    respx.get(USER_INFO).mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(InoreaderError, match="injoignable"):
        await client.get("user-info")


async def test_missing_app_key_is_reported(client, settings, authorised, clock):
    settings.inoreader_app_key = None
    clock.now = NOW + 7200
    with pytest.raises(NotAuthorized):
        await client.get("user-info")
